# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree. An additional grant of patent rights
# can be found in the PATENTS file in the same directory.

import torch
from torch import nn
from torch.nn import Parameter
import torch.nn.functional as F

from fairseq import utils

_VALID_REDUCER = {}


def register_reducer(name):
    def register_reducer_fn(fn):
        _VALID_REDUCER[name] = fn
        return fn

    return register_reducer_fn


class Reducer(nn.Module):
    """
    Dimension reducer that reduces source/target dimension of the input representation.
    """
    VALID_REDUCER = {}

    def __init__(self, method: str, reduce_src: bool, *args, **kwargs) -> None:
        super().__init__()
        self.method = method
        self.reduce_src = reduce_src
        self.specific_repr = None
        self.customize_forward = self.VALID_REDUCER[self.method](self, *args, **kwargs)

    def extra_repr(self):
        general_repr = 'method={}, reduce_src={},'.format(self.method, self.reduce_src)
        specific_repr = f' {self.specific_repr},' if self.specific_repr is not None else ''
        return general_repr + specific_repr

    def forward(self, *args, **kwargs):
        return self.customize_forward(*args, **kwargs)

    def _prepare_repr(self, x, incremental_state=None):
        """
        Concatenate the previous representation and the current
        representation in the target dimension.
        :param x: torch.FloatTensor, T x S x B x C
        :param incremental_state: Dictionary
        :return: torch.FloatTensor, T x S x B x C
        """
        if incremental_state is not None:
            saved_state = self._get_input_buffer(incremental_state)

            if 'prev_repr' in saved_state:
                x = torch.cat((saved_state['prev_repr'], x), dim=0)
            saved_state['prev_repr'] = x

            self._set_input_buffer(incremental_state, saved_state)
        return x

    def reorder_incremental_state(self, incremental_state, new_order):
        """Reorder buffered internal state (for incremental generation)."""
        input_buffer = self._get_input_buffer(incremental_state)
        if input_buffer is not None:
            for k in input_buffer.keys():
                # 2 is the Batch dim
                input_buffer[k] = input_buffer[k].index_select(2, new_order.to(input_buffer[k].device))
            self._set_input_buffer(incremental_state, input_buffer)

    def _get_input_buffer(self, incremental_state):
        return utils.get_incremental_state(
            self,
            incremental_state,
            'repr_state',
        ) or {}

    def _set_input_buffer(self, incremental_state, buffer):
        utils.set_incremental_state(
            self,
            incremental_state,
            'repr_state',
            buffer,
        )

    @register_reducer('max')
    def max(self, *args, **kwargs):
        """
        Retain only the maximum elements over the given dimension.
        :param args: Tuple
        :param kwargs: Dictionary
        :return: Callable
        """

        def reduce_src(x, mask, incremental_state=None):
            """
            Customized forward function
            :param x: torch.FloatTensor, T x S x B x C
            :param mask: torch.ByteTensor, B x S, masked elements indicated by 1
            :param incremental_state: Dictionary
            :return: torch.FloatTensor, T x B x C
            """
            # T x S x B x C
            if mask is not None:
                mask = mask.transpose(0, 1).unsqueeze(0).unsqueeze(-1)
                x = x.masked_fill(
                    mask,
                    float('-inf'),
                )
            # T x B x C
            x = x.max(dim=1)[0]
            return x

        def reduce_tgt(x, mask, incremental_state=None):
            """
            Customized forward function
            :param x: torch.FloatTensor, T x S x B x C
            :param mask: torch.ByteTensor, T x T, masked elements indicated by -inf
            :param incremental_state: Dictionary
            :return: torch.FloatTensor, S x B x C
            """
            x = self._prepare_repr(x, incremental_state)
            if mask is None:
                # decoding
                x = x.max(dim=0)[0]
            else:
                # training
                out = x[:1, :, :, :]
                for i in range(1, x.size(0)):
                    out = torch.cat(
                        (out, torch.max(x[i, :, :, :], out[i - 1, :, :, :]).unsqueeze(0)),
                        dim=0)
                x = out
            return x

        return reduce_src if self.reduce_src else reduce_tgt

    @register_reducer('attn')
    def attn(self, flags, *args, **kwargs):
        """
        Reduce the given dimension based on the distribution computed by an affine transformation.
        :param flags: Namespace
        :param args: Tuple
        :param kwargs: Dictionary
        :return: Callable
        """
        self.weights = Parameter(torch.Tensor(flags.decoder_model_dim, flags.decoder_model_dim))
        nn.init.normal_(self.weights, mean=0, std=flags.decoder_model_dim ** -0.5)

        def reduce_src(x, mask, incremental_state=None):
            """
            Customized forward function
            :param x: torch.FloatTensor, T x S x B x C
            :param mask: torch.ByteTensor, B x S, masked elements indicated by 1
            :param incremental_state: Dictionary
            :return: torch.FloatTensor, T x B x C
            """
            # T x S x B x C
            weights = F.linear(x, self.weights)
            if mask is not None:
                mask = mask.transpose(0, 1).unsqueeze(0).unsqueeze(-1)
                weights = weights.masked_fill(
                    mask,
                    float('-inf'),
                )
            prob = F.softmax(weights, dim=1)
            assert torch.isnan(prob).byte().any() == 0
            x = torch.sum(prob * x, dim=1)
            return x

        def reduce_tgt(x, mask, incremental_state=None):
            """
            Customized forward function
            :param x: torch.FloatTensor, T x S x B x C
            :param mask: torch.ByteTensor, T x T, masked elements indicated by -inf
            :param incremental_state: Dictionary
            :return: torch.FloatTensor, S x B x C
            """
            raise NotImplementedError(f'{self.method} consumes too much memory')

        return reduce_src if self.reduce_src else reduce_tgt

    @register_reducer('softmax')
    def softmax(self, *args, **kwargs):
        """
        Retain the maximum elements over the given dimension via a soft way.
        :param args: Tuple
        :param kwargs: Dictionary
        :return: Callable
        """

        def reduce_src(x, mask, incremental_state=None):
            """
            Customized forward function
            :param x: torch.FloatTensor, T x S x B x C
            :param mask: torch.ByteTensor, B x S, masked elements indicated by 1
            :param incremental_state: Dictionary
            :return: torch.FloatTensor, T x B x C
            """
            weights = x
            # T x S x B x C
            if mask is not None:
                mask = mask.transpose(0, 1).unsqueeze(0).unsqueeze(-1)
                weights = weights.masked_fill(
                    mask,
                    float('-inf'),
                )
            # T x S x B x C
            prob = F.softmax(weights, dim=1)
            assert torch.isnan(prob).byte().any() == 0
            # T x B x C
            x = torch.sum(prob * x, dim=1)
            assert torch.isnan(x).byte().any() == 0
            return x

        def reduce_tgt(x, mask, incremental_state=None):
            """
            Customized forward function
            :param x: torch.FloatTensor, T x S x B x C
            :param mask: torch.ByteTensor, T x T, masked elements indicated by -inf
            :param incremental_state: Dictionary
            :return: torch.FloatTensor, S x B x C
            """
            x = self._prepare_repr(x, incremental_state)
            if mask is None:
                # decoding
                # T x S x B x C
                prob = F.softmax(x, dim=0)
                assert torch.isnan(prob).byte().any() == 0
                # T x B x C
                x = torch.sum(prob * x, dim=0)
            else:
                # training
                out = x[:1, :, :, :]
                for i in range(1, x.size(0)):
                    out = torch.cat(
                        (out, torch.sum(
                            F.softmax(x[:i + 1, :, :, :], dim=0)
                            * x[:i + 1, :, :, :], dim=0).unsqueeze(0)),
                        dim=0)
                x = out
            return x

        return reduce_src if self.reduce_src else reduce_tgt


Reducer.VALID_REDUCER = _VALID_REDUCER