#
# Copyright © 2024 Intel Corporation
# SPDX-License-Identifier: Apache 2.0
#

from intel_npu_acceleration_library.quantization import quantize_tensor, compress_to_i4
from intel_npu_acceleration_library.nn.autograd import AutogradMatMul
from intel_npu_acceleration_library.backend import run_matmul
from intel_npu_acceleration_library.dtypes import NPUDtype
from typing import Optional, Union
import torch
import uuid
import math


class Linear(torch.nn.Module):
    """Torch Linear operation NPU backend."""

    def __init__(self, weight: torch.Tensor, bias: Optional[torch.Tensor] = None):
        """Initialize the Linear class.

        Args:
            weight (torch.Tensor): Linear operation weight
            bias (Optional[torch.Tensor], optional): Linear operation optional bias. Defaults to None.
        """
        super().__init__()

        self.weight = torch.nn.Parameter(weight)
        self.bias = torch.nn.Parameter(bias) if isinstance(bias, torch.Tensor) else None
        self.outC, self.inC = self.weight.shape
        self.op_id = str(uuid.uuid4())
        # assert self.weight.dtype == torch.float16
        self._mm = AutogradMatMul.apply

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Torch module forward method.

        Args:
            x (torch.Tensor): Input tensor

        Returns:
            torch.Tensor: result
        """
        if self.training:
            out = self._mm(x, self.weight, None)
        else:
            out = run_matmul(x, self.weight, None, self.op_id)

        if self.bias is None:
            return out
        return out + self.bias

    @staticmethod
    def fromTorch(
        layer: torch.nn.Linear, dtype: torch.dtype = torch.float16
    ) -> Union["Linear", "QuantizedLinear"]:
        """Generate a NPU Linear layer from a torch one.

        Args:
            layer (torch.nn.Linear): the original torch.nn.Linear model to run on the NPU
            dtype (torch.dtype): the desired datatype

        Returns:
            Union[Linear, QuantizedLinear]: A NPU linear layer
        """
        if any(dim > 2**17 for dim in layer.weight.shape):
            return layer
        return Linear.fromTensor(layer.weight, getattr(layer, "bias", None), dtype)

    @staticmethod
    def fromTensor(
        weight: torch.Tensor,
        bias: Optional[torch.Tensor],
        dtype: torch.dtype = torch.float16,
    ) -> Union["Linear", "QuantizedLinear"]:
        """Generate a NPU Linear layer from a torch one.

        Args:
            weight (torch.Tensor): the original weight tensor
            bias (Optional[torch.Tensor]): the original bias tensor
            dtype (torch.dtype): the desired datatype

        Raises:
            RuntimeError: dtype not supported

        Returns:
            Union[Linear, QuantizedLinear]: A NPU linear layer
        """
        if dtype.is_floating_point:
            if bias is None:
                return Linear(weight.to(dtype), None)
            return Linear(weight.to(dtype), bias.to(dtype))
        elif isinstance(dtype, NPUDtype):
            weights_quant, scale = quantize_tensor(weight, (dtype.min, dtype.max))
            if dtype.bits == 4:
                weights_quant = compress_to_i4(weights_quant)
            return QuantizedLinear(weights_quant, scale, bias)
        elif dtype == torch.int8:
            weights_quant, scale = quantize_tensor(weight)
            return QuantizedLinear(weights_quant, scale, bias)
        else:
            raise RuntimeError(
                f"intel-npu-acceleration-library library do not support yet the requeste datatype: {dtype}"
            )


class QuantizedLinear(torch.nn.Module):
    """Torch Quantized Linear operation NPU backend."""

    def __init__(
        self,
        weight: torch.Tensor,
        scale: torch.Tensor,
        bias: Optional[torch.Tensor] = None,
    ):
        """Initialize the QuantizedLinear class.

        Args:
            weight (torch.Tensor): Linear operation weight
            scale (torch.Tensor): Quantization scale
            bias (Optional[torch.Tensor], optional): Linear operation optional bias. Defaults to None.

        Raises:
            RuntimeError: Quantized weight must be in torch.int8 format
        """
        super().__init__()

        self.weight = weight
        if self.weight.dtype not in (torch.int8, torch.uint8):
            raise RuntimeError(
                f"Quantized weight must be in torch.(u)int8 dtype instead of {self.weight.dtype}"
            )
        self.outC, self.inC = self.weight.shape
        if self.weight.dtype == torch.uint8:
            # In case is Int4 we need to double the input channels because weights are compressed
            self.inC *= 2
        self.scale = scale * math.sqrt(self.inC)
        self.bias = bias
        self.op_id = str(uuid.uuid4())
        self._mm = AutogradMatMul.apply

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Torch module forward method.

        Args:
            x (torch.Tensor): Input tensor

        Raises:
            RuntimeError: Training is not supported for QuantizedLinear layer. Use `.eval()` to do inference only

        Returns:
            torch.Tensor: result
        """
        if self.training:
            raise RuntimeError(
                "Training is not supported for QuantizedLinear layer. Use `.eval()` to do inference only"
            )
        out = run_matmul(x, self.weight, self.scale, self.op_id)

        if self.bias is None:
            return out
        return out + self.bias
