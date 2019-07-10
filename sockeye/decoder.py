# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not
# use this file except in compliance with the License. A copy of the License
# is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

"""
Decoders for sequence-to-sequence models.
"""
import logging
from abc import abstractmethod
from typing import Dict, List, Optional, Tuple, Union, Type

import mxnet as mx

from . import constants as C
from . import layers
from . import transformer

logger = logging.getLogger(__name__)
DecoderConfig = Union[transformer.TransformerConfig]


def get_decoder(config: DecoderConfig, prefix: str = '') -> 'Decoder':
    return Decoder.get_decoder(config, prefix)


class Decoder(mx.gluon.Block):
    """
    Generic decoder interface.
    A decoder needs to implement code to decode a target sequence known in advance (decode_sequence),
    and code to decode a single word given its decoder state (decode_step).
    The latter is typically used for inference graphs in beam search.
    For the inference module to be able to keep track of decoder's states
    a decoder provides methods to return initial states (init_states), state variables and their shapes.
    """

    __registry = {}  # type: Dict[Type[DecoderConfig], Tuple[Type['Decoder'], str]]

    @classmethod
    def register(cls, config_type: Type[DecoderConfig], suffix: str):
        """
        Registers decoder type for configuration. Suffix is appended to decoder prefix.

        :param config_type: Configuration type for decoder.
        :param suffix: String to append to decoder prefix.

        :return: Class decorator.
        """
        def wrapper(target_cls):
            cls.__registry[config_type] = (target_cls, suffix)
            return target_cls

        return wrapper

    @classmethod
    def get_decoder(cls, config: DecoderConfig, prefix: str) -> 'Decoder':
        """
        Creates decoder based on config type.

        :param config: Decoder config.
        :param prefix: Prefix to prepend for decoder.

        :return: Decoder instance.
        """
        config_type = type(config)
        if config_type not in cls.__registry:
            raise ValueError('Unsupported decoder configuration %s' % config_type.__name__)
        decoder_cls, suffix = cls.__registry[config_type]
        # TODO: move final suffix/prefix construction logic into config builder
        return decoder_cls(config=config, prefix=prefix + suffix)

    @abstractmethod
    def __init__(self):
        super().__init__()

    @abstractmethod
    def init_state_from_encoder(self,
                                encoder_outputs: mx.nd.NDArray,
                                encoder_valid_length: Optional[mx.nd.NDArray] = None,
                                is_inference: bool = True) -> List[mx.nd.NDArray]:
        raise NotImplementedError()

    @abstractmethod
    def decode_seq(self, inputs: mx.nd.NDArray, states: List[mx.nd.NDArray]):
        """
        Decodes a sequence of embedded target words and returns sequence of last decoder
        representations for each time step.

        :param inputs: Encoded source: (batch_size, source_encoded_max_length, encoder_depth).
        :param states: List of initial states, as given by init_state_from_encoder().
        :return: Decoder output. Shape: (batch_size, target_embed_max_length, decoder_depth).
        """
        raise NotImplementedError()

    @abstractmethod
    def get_num_hidden(self):
        raise NotImplementedError()


@Decoder.register(transformer.TransformerConfig, C.TRANSFORMER_DECODER_PREFIX)
class TransformerDecoder(Decoder, mx.gluon.HybridBlock):
    """
    Transformer decoder as in Vaswani et al, 2017: Attention is all you need.
    In training, computation scores for each position of the known target sequence are compouted in parallel,
    yielding most of the speedup.
    At inference time, the decoder block is evaluated again and again over a maximum length input sequence that is
    initially filled with zeros and grows during beam search with predicted tokens. Appropriate masking at every
    time-step ensures correct self-attention scores and is updated with every step.

    :param config: Transformer configuration.
    :param prefix: Name prefix for symbols of this decoder.
    """

    def __init__(self,
                 config: transformer.TransformerConfig,
                 prefix: str = C.TRANSFORMER_DECODER_PREFIX) -> None:
        Decoder.__init__(self)
        mx.gluon.HybridBlock.__init__(self, prefix=prefix)
        self.config = config
        with self.name_scope():
            self.pos_embedding = layers.PositionalEmbeddings(weight_type=self.config.positional_embedding_type,
                                                             num_embed=self.config.model_size,
                                                             max_seq_len=self.config.max_seq_len_target,
                                                             prefix=C.TARGET_POSITIONAL_EMBEDDING_PREFIX,
                                                             scale_up_input=True,
                                                             scale_down_positions=False)
            self.autoregressive_bias = transformer.AutoRegressiveBias(prefix="autoregressive_bias_")
            self.valid_length_mask = transformer.TransformerValidLengthMask(num_heads=self.config.attention_heads,
                                                                            fold_heads=False,
                                                                            name="bias")
            self.layers = mx.gluon.nn.HybridSequential()
            for i in range(config.num_layers):
                self.layers.add(transformer.TransformerDecoderBlock(config, prefix="%d_" % i))

            self.final_process = transformer.TransformerProcessBlock(sequence=config.preprocess_sequence,
                                                                     dropout=config.dropout_prepost,
                                                                     prefix="final_process_",
                                                                     num_hidden=self.config.model_size)

    def init_state_from_encoder(self,
                                encoder_outputs: mx.nd.NDArray,
                                encoder_valid_length: Optional[mx.nd.NDArray] = None,
                                is_inference: bool = True) -> List[mx.nd.NDArray]:
        """
        Returns the initial states given encoder output. States for teacher-forced training are encoder outputs
        and a valid length mask for encoder outputs.
        At inference, this method returns the following state tuple:
        valid length bias, step state,
        [projected encoder attention keys, projected encoder attention values] * num_layers,
        [self attention dummies] * num_layers.

        :param encoder_outputs: Encoder outputs. Shape: (batch, source_length, encoder_dim).
        :param encoder_valid_length: Valid lengths of encoder outputs. Shape: (batch,).
        :param is_inference: Whether to return states for inference or for training.
        :return: Initial states.
        """
        source_mask = self.valid_length_mask(encoder_outputs, encoder_valid_length)

        if is_inference:

            step = mx.nd.zeros_like(encoder_valid_length)
            states = [source_mask, step]

            for layer in self.layers:
                encoder_attention_keys = layer.enc_attention.ff_k(encoder_outputs)
                encoder_attention_values = layer.enc_attention.ff_v(encoder_outputs)
                states.append(encoder_attention_keys)
                states.append(encoder_attention_values)

            batch_size = encoder_outputs.shape[0]
            self_attention_key_value_dummies = [mx.nd.zeros((batch_size, 1, self.config.model_size),
                                                            ctx=encoder_outputs.context,
                                                            dtype=encoder_outputs.dtype)] * self.config.num_layers * 2
            states += self_attention_key_value_dummies

        else:
            states = [source_mask, encoder_outputs]

        return states

    def decode_seq(self, inputs: mx.nd.NDArray, states: List[mx.nd.NDArray]):
        """
        Decodes a sequence of embedded target words and returns sequence of last decoder
        representations for each time step.

        :param inputs: Encoded source: (batch_size, source_encoded_max_length, encoder_depth).
        :param states: List of initial states, as given by init_state_from_encoder().
        :return: Decoder output. Shape: (batch_size, target_embed_max_length, decoder_depth).
        """
        # TODO: should we return the states?
        outputs, _ = self.forward(inputs, states)
        return outputs

    def forward(self, step_input, states):
        """
        Run forward pass of the decoder.

        step_input is either:
             (batch, num_hidden): single decoder step at inference time
             (batch, seq_len, num_hidden): full sequence decode during training.

        states is either:
             len(states) == 3: encoder_outputs, source_bias, step
             len(states) > 3: encoder_outputs, source_bias, step, layer_caches...
        """
        input_shape = step_input.shape

        is_inference = len(input_shape) == 2

        if is_inference:
            # Just add the length dimension:
            # (batch, num_hidden) -> (batch, 1, num_hidden)
            step_input = mx.nd.expand_dims(step_input, axis=1)

        # run decoder op
        target, self_attention_key_values = super().forward(step_input, states)

        if is_inference:
            # During inference, length dimension of decoder output has size 1, squeeze it
            # (batch, num_hidden)
            target = target.squeeze()
            # We also increment time step state (2nd state in the list) and add new caches
            step = states[1] + 1
            # constant encoder attention keys & values
            encoder_attention_keys_values = states[2:2 + self.config.num_layers * 2]
            new_states = [states[0], step] + encoder_attention_keys_values + self_attention_key_values
        else:
            new_states = None  # we don't care about states in training

        return target, new_states

    def hybrid_forward(self, F, step_input, states):
        # unpack states list
        is_training = len(states) == 2
        is_inference = len(states) == 2 + self.config.num_layers * 4

        if is_training:
            source_mask, source_encoded = states
            mask = self.autoregressive_bias(step_input)  # mask: (1, length, length)
            step = None  # no step information required at training
            enc_att_kv = [(None, None) for _ in range(self.config.num_layers)]  # no self-attention caching
            self_att_kv = [(None, None) for _ in range(self.config.num_layers)]  # no self-attention caching

        elif is_inference:
            source_mask, step, *other = states
            source_encoded = None  # use constant pre-computed key value projections from the states
            mask = None  # no autoregressive bias needed at inference
            enc_att_kv = other[:self.config.num_layers * 2]
            enc_att_kv = [enc_att_kv[i:i + 2] for i in range(0, len(enc_att_kv), 2)]
            self_att_kv = other[self.config.num_layers * 2:]
            self_att_kv = [self_att_kv[i:i + 2] for i in range(0, len(self_att_kv), 2)]

        else:
            raise ValueError("Invalid state list")

        # Fold the heads of source_mask (batch_size, num_heads, seq_len) -> (batch_size * num_heads, 1, seq_len)
        source_mask = F.expand_dims(F.reshape(source_mask, shape=(-3, -2)), axis=1)

        # target: (batch_size, length, model_size)
        target = self.pos_embedding(step_input, step)

        if self.config.dropout_prepost > 0.0:
            target = F.Dropout(data=target, p=self.config.dropout_prepost)

        new_self_att_kv = []  # type: List[Tuple]
        for layer, (self_att_k, self_att_v), (enc_att_k, enc_att_v) in zip(self.layers, self_att_kv, enc_att_kv):
            target, new_self_att_k, new_self_att_v = layer(target,
                                                           mask,
                                                           source_encoded,
                                                           source_mask,
                                                           self_att_k, self_att_v,
                                                           enc_att_k, enc_att_v)
            new_self_att_kv += [new_self_att_k, new_self_att_v]
        target = self.final_process(target, None)

        return target, new_self_att_kv

    def get_num_hidden(self):
        return self.config.model_size
