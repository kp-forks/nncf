# Copyright (c) 2025 Intel Corporation
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#      http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import List

import pytest
import torch
import torch.nn as nn
import torch.nn.functional as F

import nncf
from nncf import BackupMode
from nncf import CompressWeightsMode
from nncf import SensitivityMetric
from nncf.quantization import compress_weights
from nncf.quantization.advanced_parameters import AdvancedCompressionParameters
from nncf.tensor import Tensor
from nncf.tensor import TensorDataType
from nncf.torch import wrap_model
from nncf.torch.quantization.layers import INT4AsymmetricWeightsDecompressor
from nncf.torch.quantization.layers import INT4SymmetricWeightsDecompressor
from nncf.torch.quantization.layers import INT8AsymmetricWeightsDecompressor
from nncf.torch.quantization.layers import INT8SymmetricWeightsDecompressor
from nncf.torch.quantization.quantize_functions import pack_int4
from nncf.torch.quantization.quantize_functions import pack_uint4
from nncf.torch.quantization.quantize_functions import unpack_int4
from nncf.torch.quantization.quantize_functions import unpack_uint4
from tests.cross_fw.test_templates.template_test_weights_compression import TemplateWeightCompression
from tests.torch.test_models.synthetic import ShortTransformer
from tests.torch.test_tensor import cast_to

ALL_SENSITIVITY_METRICS = list(SensitivityMetric)

INT8_MODES = (CompressWeightsMode.INT8_ASYM, CompressWeightsMode.INT8_SYM)
INT4_MODES = (CompressWeightsMode.INT4_SYM, CompressWeightsMode.INT4_ASYM)
SUPPORTED_MODES = INT8_MODES + INT4_MODES
UNSUPPORTED_MODES = (CompressWeightsMode.NF4, CompressWeightsMode.E2M1)


class SequentialMatmulModel(nn.Module):
    def __init__(self):
        super(SequentialMatmulModel, self).__init__()
        self.main_values = [10000, 1000, 1, 10, 10000]
        self.layers = nn.ModuleList()

        for _, main_value in enumerate(self.main_values):
            weights_data = torch.arange(0, 16, dtype=torch.float32).reshape(4, 4)
            weights_data[-1, -1] = main_value
            weight_tensor = torch.tensor(weights_data)
            layer = nn.Linear(4, 4, bias=False)
            layer.weight = nn.Parameter(weight_tensor.t())
            self.layers.append(layer)

    def forward(self, x):
        for layer in self.layers:
            x = layer(x)
        return x

    def get_weight_names_in_exec_order(self):
        return [f"{i}_weight" for i in range(len(self.main_values))]


class MatMulModel(torch.nn.Module):
    def __init__(self, weight: torch.Tensor = torch.ones(size=(256, 256), dtype=torch.float32)):
        super().__init__()
        self.w = torch.nn.Parameter(weight)

    def forward(self, input):
        return input @ self.w


class LinearModel(torch.nn.Module):
    def __init__(self, weight: torch.Tensor = torch.ones(size=(256, 256), dtype=torch.float32)):
        super().__init__()
        self.linear = torch.nn.Linear(weight.shape[0], weight.shape[1], False)
        self.linear.weight = torch.nn.Parameter(weight)

    def forward(self, input):
        return self.linear(input)


class FunctionalModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.conv_w = torch.nn.Parameter(torch.ones(size=(5, 3, 3, 3), dtype=torch.float32))
        self.matmul_w = torch.nn.Parameter(torch.ones(size=(1, 3, 256, 256), dtype=torch.float32))
        self.conv_tr_w = torch.nn.Parameter(torch.rand(size=(5, 4, 3, 3)))
        self.nested_matmul = MatMulModel()

    def forward(self, input_):
        x = input_.to(torch.float32)
        x = x @ self.matmul_w
        x = self.nested_matmul(x)
        x = F.conv2d(x, self.conv_w)
        x = F.conv_transpose2d(x, self.conv_tr_w)
        return x


class ConvolutionModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.conv_regular = torch.nn.Conv2d(in_channels=3, out_channels=16, kernel_size=3)
        self.max_pool2d = torch.nn.MaxPool2d(kernel_size=2)
        self.conv_transpose = torch.nn.ConvTranspose2d(in_channels=16, out_channels=8, kernel_size=3)
        self.conv_depthwise = torch.nn.Conv2d(in_channels=8, out_channels=8, kernel_size=5, groups=8)
        self.adaptive_avg_pool = torch.nn.AdaptiveAvgPool2d(output_size=1)
        self.linear = torch.nn.Linear(in_features=8, out_features=8)

    def forward(self, input_):
        input_ = input_.to(torch.float32)
        x = self.conv_regular(input_)
        x = F.relu(x)
        x.transpose_(2, 3)
        x = self.max_pool2d(x)
        y = self.conv_transpose(x)
        z = F.conv_transpose2d(x, self.conv_transpose.weight)
        x = y + z
        x = self.conv_depthwise(x)
        x = F.conv2d(x, self.conv_depthwise.weight, groups=self.conv_depthwise.groups)
        x += torch.ones_like(x)
        x = self.adaptive_avg_pool(x)
        x = self.linear(x.flatten())
        return x


@pytest.mark.parametrize("mode", SUPPORTED_MODES)
def test_compress_weights(mode):
    model = ShortTransformer(8, 16)
    dtype = torch.int8 if mode == CompressWeightsMode.INT8_SYM else torch.uint8

    input_ids = torch.randint(0, 10, (8,))
    wrapped_model = wrap_model(model, example_input=input_ids, trace_parameters=True)

    kwargs = {}
    if mode in [CompressWeightsMode.INT4_SYM, CompressWeightsMode.INT4_ASYM]:
        kwargs["group_size"] = 4
    compressed_model = compress_weights(wrapped_model, mode=mode, **kwargs)

    n_compressed_weights = 0
    n_target_modules = 0

    for _, module in compressed_model.named_children():
        if isinstance(module, (torch.nn.Linear, torch.nn.Embedding)):
            n_target_modules += 1
            if module.weight.dtype == dtype:
                n_compressed_weights += 1

    assert n_compressed_weights == n_target_modules


@pytest.mark.parametrize("mode", SUPPORTED_MODES)
def test_compress_weights_functional_model(mode):
    model = FunctionalModel()
    decompressor_map = {
        CompressWeightsMode.INT8_SYM: (INT8SymmetricWeightsDecompressor,),
        CompressWeightsMode.INT8_ASYM: (INT8AsymmetricWeightsDecompressor,),
        CompressWeightsMode.INT4_SYM: (INT4SymmetricWeightsDecompressor, INT8AsymmetricWeightsDecompressor),
        CompressWeightsMode.INT4_ASYM: (INT4AsymmetricWeightsDecompressor, INT8AsymmetricWeightsDecompressor),
    }

    decompressor_type = decompressor_map[mode]

    input_ids = torch.randint(0, 10, [1, 3, 256, 256])
    wrapped_model = wrap_model(model, example_input=input_ids, trace_parameters=True)
    compressed_model = compress_weights(wrapped_model, mode=mode)

    n_compressed_weights = 0
    for layer in compressed_model.nncf.external_op.values():
        if isinstance(layer, decompressor_type):
            n_compressed_weights += 1
    assert n_compressed_weights == 4


def test_compress_weights_conv():
    model = ConvolutionModel()

    input_ids = torch.randint(0, 10, [1, 3, 300, 300])
    wrapped_model = wrap_model(model, example_input=input_ids, trace_parameters=True)
    compressed_model = compress_weights(wrapped_model)

    n_compressed_weights = 0
    n_target_modules = 0

    for _, module in compressed_model.named_children():
        if isinstance(module, (torch.nn.Linear, torch.nn.Conv2d, torch.nn.ConvTranspose2d)):
            n_target_modules += 1
            if module.weight.dtype in [torch.uint8, torch.int8]:
                n_compressed_weights += 1

    assert n_compressed_weights == n_target_modules


@pytest.mark.parametrize("mode", SUPPORTED_MODES)
def test_compress_shared_weights(mocker, mode):
    model = ShortTransformer(8, 16, share_weights=True)
    dtype = torch.int8 if mode == CompressWeightsMode.INT8_SYM else torch.uint8

    input_ids = torch.randint(0, 10, (8,))
    wrapped_model = wrap_model(model, example_input=input_ids, trace_parameters=True)

    kwargs = {}
    if mode in [CompressWeightsMode.INT4_SYM, CompressWeightsMode.INT4_ASYM]:
        kwargs["group_size"] = 4
    compressed_model = compress_weights(wrapped_model, mode=mode, **kwargs)

    n_compressed_weights = 0
    n_target_modules = 0

    for _, module in compressed_model.named_children():
        if isinstance(module, (torch.nn.Linear, torch.nn.Embedding)):
            n_target_modules += 1
            if module.weight.dtype == dtype:
                n_compressed_weights += 1

    assert n_compressed_weights == n_target_modules
    assert len(compressed_model.nncf.external_op) == 2

    # check that the weight decompressors are called only once
    for val in compressed_model.nncf.external_op.values():
        mocker.spy(val, "forward")

    compressed_model(input_ids)

    for val in compressed_model.nncf.external_op.values():
        assert val.forward.call_count == 1


class EmptyModel(torch.nn.Module):
    def forward(self, input):
        return input


@pytest.mark.parametrize("mode", INT8_MODES)
@pytest.mark.parametrize(
    "params",
    (
        {"ratio": 0.5},
        {"group_size": 64},
        {"all_layers": True},
        {"all_layers": False},
        *({"sensitivity_metric": metric} for metric in ALL_SENSITIVITY_METRICS),
        {"gptq": True},
        {"awq": True},
        {"scale_estimation": True},
        {"lora_correction": True},
        {"backup_mode": BackupMode.NONE},
        {"backup_mode": BackupMode.INT8_ASYM},
        {"backup_mode": BackupMode.INT8_SYM},
        {"advanced_parameters": AdvancedCompressionParameters(statistics_path="anything")},
    ),
)
def test_raise_error_with_unsupported_params_for_int8(mode, params):
    dummy_torch_model = EmptyModel()
    dummy_input = torch.Tensor()
    wrapped_model = wrap_model(dummy_torch_model, example_input=dummy_input, trace_parameters=True)
    with pytest.raises(nncf.ParameterNotSupportedError):
        compress_weights(wrapped_model, mode=mode, **params)


@pytest.mark.parametrize("mode", INT4_MODES)
@pytest.mark.parametrize(
    "params",
    (
        {"gptq": True},
        {"awq": True},
        {"lora_correction": True},
    ),
)
def test_raise_error_with_unsupported_params_for_int4(mode, params):
    dummy_torch_model = EmptyModel()
    dummy_input = torch.Tensor()
    wrapped_model = wrap_model(dummy_torch_model, example_input=dummy_input, trace_parameters=True)
    with pytest.raises(nncf.ParameterNotSupportedError):
        compress_weights(wrapped_model, mode=mode, **params)


@pytest.mark.parametrize("mode", UNSUPPORTED_MODES)
def test_raise_error_with_not_int8(mode):
    dummy_torch_model = EmptyModel()
    dummy_input = torch.Tensor()
    wrapped_model = wrap_model(dummy_torch_model, example_input=dummy_input, trace_parameters=True)
    with pytest.raises(nncf.ParameterNotSupportedError):
        compress_weights(wrapped_model, mode=mode)


def test_raise_error_for_statistics_caching():
    dummy_torch_model = EmptyModel()
    dummy_input = torch.Tensor()
    wrapped_model = wrap_model(dummy_torch_model, example_input=dummy_input, trace_parameters=True)
    with pytest.raises(nncf.ParameterNotSupportedError):
        compress_weights(wrapped_model, advanced_parameters=AdvancedCompressionParameters(statistics_path="anything"))


class DTypeModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.weight = torch.nn.Parameter(torch.ones(size=(3, 3), dtype=torch.float32))

    def forward(self, x):
        x = x.to(self.weight.dtype)
        x = x @ self.weight
        return x


def test_get_dtype_attribute_of_parameter():
    model = DTypeModel()
    dummy_input = torch.randint(0, 10, [3, 3])
    wrapped_model = wrap_model(model, example_input=dummy_input, trace_parameters=True)
    compressed_model = compress_weights(wrapped_model)
    assert compressed_model.weight.dtype == torch.uint8
    compressed_model(dummy_input)
    assert compressed_model.weight.dtype == torch.uint8


@pytest.mark.parametrize("dtype", ("float16", "float32"))
def test_model_devices_and_precisions(use_cuda, dtype):
    if use_cuda and not torch.cuda.is_available():
        pytest.skip("Skipping for CPU-only setups")
    device = torch.device("cuda" if use_cuda else "cpu")
    dtype = torch.float16 if dtype == "float16" else torch.float32

    model = MatMulModel().to(device)
    if dtype == torch.float16:
        model.half()

    dummy_input = torch.rand((1, 256), dtype=dtype, device=device)
    wrapped_model = wrap_model(model, example_input=dummy_input, trace_parameters=True)
    compressed_model = compress_weights(wrapped_model)
    result = compressed_model(dummy_input)

    # Scale should always be in float16
    assert compressed_model.state_dict()["_nncf.external_op.weights_decompressor_w._scale"].dtype == torch.float16
    # Result should be in the precision of the model
    assert result.dtype == dtype


def test_pack_uint4():
    w_uint8 = torch.randint(0, 15, (4, 4), dtype=torch.uint8)
    packed_w = pack_uint4(w_uint8)
    assert packed_w.dtype == torch.uint8
    assert packed_w.numel() * 2 == w_uint8.numel()
    unpacked_w = unpack_uint4(packed_w).reshape(w_uint8.shape)
    assert torch.all(unpacked_w == w_uint8)


def test_pack_int4():
    w_int8 = torch.randint(-8, 7, (4, 4), dtype=torch.int8)
    packed_w = pack_int4(w_int8)
    assert packed_w.dtype == torch.uint8
    assert packed_w.numel() * 2 == w_int8.numel()
    unpacked_w = unpack_int4(packed_w).reshape(w_int8.shape)
    assert torch.all(unpacked_w == w_int8)


class TestPTTemplateWeightCompression(TemplateWeightCompression):
    @staticmethod
    def get_matmul_model() -> torch.nn.Module:
        return MatMulModel(255 * torch.eye(3, dtype=torch.float32))

    @staticmethod
    def get_sequential_matmul_model() -> torch.nn.Module:
        return SequentialMatmulModel()

    @staticmethod
    def to_tensor(t) -> torch.Tensor:
        return torch.tensor(t)

    @staticmethod
    def cast_to(x: torch.Tensor, dtype: TensorDataType) -> torch.Tensor:
        return cast_to(x, dtype)

    @staticmethod
    def check_weights(model: torch.nn.Module, ref_ids: List[int]) -> None:
        all_names = model.get_weight_names_in_exec_order()
        low_precision_nodes = list(map(lambda i: all_names[i], ref_ids))
        for op_name, op in model.nncf.external_op.items():
            for name in low_precision_nodes:
                if name in op_name:
                    assert isinstance(op, INT4SymmetricWeightsDecompressor)

    @staticmethod
    def get_model_for_test_scale_estimation():
        return LinearModel(torch.arange(0, 8 * 16, dtype=torch.float32).reshape(16, 8))

    @staticmethod
    def get_scale_estimation_ref():
        return torch.tensor(
            [
                [[0.473328]],
                [[0.929023]],
                [[1.446527]],
                [[1.920595]],
                [[2.517054]],
                [[3.030102]],
                [[3.584279]],
                [[4.043509]],
                [[4.620008]],
                [[5.165322]],
                [[5.710637]],
                [[6.122581]],
                [[6.655914]],
                [[7.237174]],
                [[7.722580]],
                [[8.255914]],
            ]
        )

    @staticmethod
    def get_orig_weight(model: torch.nn.Module) -> Tensor:
        return Tensor(model.linear.weight)

    @staticmethod
    def get_decompressed_weight(compressed_model: torch.nn.Module, input: torch.Tensor) -> Tensor:
        weight = compressed_model.linear.weight
        unpacked_w = compressed_model.nncf.external_op.weights_decompressor_linear_weight(weight)
        return Tensor(unpacked_w)
