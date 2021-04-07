# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------

from . import _utils, _ortmodule_utils, _ortmodule_output_transformation as _ortmodule_io

from onnxruntime.capi.onnxruntime_inference_collection import OrtValue
from onnxruntime.capi import _pybind_state as C

from abc import ABC, abstractmethod
import io
import inspect
import onnx
import onnxruntime
import torch
from torch.utils.cpp_extension import ROCM_HOME

ONNX_OPSET_VERSION = 12

def _run_forward(execution_session, onnx_model, device, *inputs, **kwargs):
    """Runs the forward graph on execution_session with given model inputs and device"""

    # Assert that the input and model device match
    _ortmodule_utils._check_same_device(
        device, "Input argument to forward", *inputs)

    # TODO: Try to reuse the output buffers as some of the output tensors are same sizes,
    #   especially the backward graph outputs.
    io_binding = execution_session.io_binding()
    run_options = C.RunOptions()

    # Use IO binding
    _ortmodule_utils._create_iobinding(io_binding, inputs, onnx_model, device)

    # Run and return module outputs.
    ort_output = execution_session.run_forward(io_binding, run_options)
    forward_outputs, run_id = ort_output.ortvalues, ort_output.run_id

    user_outputs = tuple(_ortmodule_utils._ortvalue_to_torch_tensor(
        forward_output) for forward_output in forward_outputs)

    # Assert that the outputs and model device match
    _ortmodule_utils._check_same_device(
        device, "Output argument from forward", *user_outputs)

    output_info = [(output.shape, output.device, output.dtype) for output in user_outputs]
    run_info = onnxruntime.training.RunStateInfo(run_id, run_options, io_binding, output_info)

    # Return user outputs and forward run information
    return user_outputs, run_info

class GraphExecutionManager(ABC):
    def __init__(self, module):
        """Manages building and execution of onnx graphs

        This class is an abstract class and should not directly be instantiated.
        Please use one of the concrete implementations of GraphExecutionManager.

        Interacts with OrtModuleGraphBuilder to build and optimize
        the onnx graph, and ExecutionAgent to run the onnx graph.
        """

        # Original and flattened (tranformed) output module
        self._original_module = module._base_module
        self._flattened_module = module

        # Exported model
        self._onnx_model = None
        
        # Model after inference optimization or gradient building.
        self._optimized_onnx_model = None
        self._graph_builder = None
        self._graph_info = None

        # TrainingAgent or InferenceAgent
        self._execution_agent = None

        # Debug flags
        self._save_onnx = False
        self._save_onnx_prefix = ''

        # Value can be either torch.onnx.TrainingMode.TRAININGor torch.onnx.TrainingMode.EVAL
        # To be instantiated in the concrete implementation of GraphExecutionManager
        self._export_mode = None

        # Related to training graph shape inference
        self._current_input_shape = None
        # default execution order is priority-based for both dynamic/static shape input for now
        # if we observe benefit of static shape, we can expose this flag to user
        self._use_static_shape = False
        self._input_names_require_grad = None
        self._module_output_schema = None

        # Verbosity for logging
        self._verbosity = _ortmodule_utils.Verbosity.WARNING

        # TODO: Single device support for now
        self._device = _utils.get_device_from_module(module)

        self._module_parameters = inspect.signature(
            self._original_module.forward).parameters.values()

        # TODO: remove after PyTorch ONNX exporter supports VAR_KEYWORD parameters.
        for input_parameter in self._module_parameters:
            if input_parameter.kind == inspect.Parameter.VAR_KEYWORD:
                raise NotImplementedError(
                    "The model's forward method has **kwargs parameter which is currently not supported.")

        self.is_rocm_pytorch = (True if (
            (torch.version.hip is not None) and (ROCM_HOME is not None)) else False)

        self._use_external_gpu_allocator = True
        if self._use_external_gpu_allocator:
            # CPP extension to get torch GPU allocator's alloc and free function addresses
            self._torch_gpu_allocator = _ortmodule_utils._load_torch_gpu_allocator_cpp_extension(self._verbosity,
                                                                                                 self.is_rocm_pytorch)
            self._torch_alloc = self._torch_gpu_allocator.gpu_caching_allocator_raw_alloc_address()
            self._torch_free = self._torch_gpu_allocator.gpu_caching_allocator_raw_delete_address()

    @abstractmethod
    def forward(self):
        """Executes the forward method for ORTModule

        This is an abstract method and must be overridden by a concrete implementation.
        This is the only method that the user should call on a concrete instance of the ExecutionManager
        All other methods are internal"""
        pass

    def _build_graph(self):
        if self._use_static_shape:
            self._graph_builder.build(
                self._current_input_shape)
        else:
            self._graph_builder.build()

        self._optimized_onnx_model = onnx.load_model_from_string(
            self._graph_builder.get_model())
        self._graph_info = \
            self._graph_builder.get_graph_info()


    def _get_session_config(self):
        """Creates and returns the session configuration to be used for the ExecutionAgent"""
        providers = None
        provider_options = None
        if self._device.type == 'cuda':
            # Configure the InferenceSessions to use the specific GPU on which the model is placed.
            providers = (["ROCMExecutionProvider"] if self.is_rocm_pytorch else [
                         "CUDAExecutionProvider"])
            providers.append("CPUExecutionProvider")
            if self._use_external_gpu_allocator:
                provider_options = [{"device_id": str(self._device.index), "gpu_external_alloc": str(
                    self._torch_alloc), "gpu_external_free": str(self._torch_free)}, {}]
            else:
                provider_options = [{"device_id": str(self._device.index)}, {}]
        elif self._device.type == 'cpu':
            providers = ["CPUExecutionProvider"]
            provider_options = [{}]

        session_options = onnxruntime.SessionOptions()
        session_options.enable_mem_pattern = False
        session_options.use_deterministic_compute = False
        # default to PRIORITY_BASED execution order
        session_options.execution_order = onnxruntime.ExecutionOrder.PRIORITY_BASED
        # 0:Verbose, 1:Info, 2:Warning. 3:Error, 4:Fatal. Default is 2.
        session_options.log_severity_level = int(self._verbosity)

        # enable dumping optimized training graph
        if self._save_onnx:
            session_options.optimized_model_filepath = self._save_onnx_prefix + '_training_optimized.onnx'

        return session_options, providers, provider_options

    def _export_model(self, *inputs, **kwargs):
        # 1. Set the self._device from the user module
        # 2. Export the user model under self._export_training_flag mode
        # Return True if the model needed to be exported, False if no export was required.
        if self._onnx_model:
            # All required models have already been exported previously
            return False

        self._set_device_from_module()
        self._onnx_model = self._get_exported_model(*inputs, **kwargs)

        return True

    def _get_exported_model(self, *inputs, **kwargs):
        '''Exports PyTorch `self._flattened_module` to ONNX for inferencing or training, using `*inputs` as input

        TODO: How to support dynamic axes? Dimensions are determined by samples
        '''

        # Setup dynamic axes for onnx model
        input_names, dynamic_axes, self._input_names_require_grad, _ = \
            _ortmodule_io.parse_inputs_for_onnx_export(
                self._module_parameters, None, *inputs, **kwargs)
        output_names, output_dynamic_axes, self._module_output_schema = \
            _ortmodule_io.parse_outputs_for_onnx_export_and_extract_output_schema(
                self._original_module, inputs, kwargs)
        dynamic_axes.update(output_dynamic_axes)

        # Export torch.nn.Module to ONNX
        f = io.BytesIO()

        # Deepcopy inputs, since input values may change after model run.
        # NOTE: Inputs may contain tensors that have attributes preventing their deepcopy (example grad_fn).
        # Therefore, deepcopy only the data component of the input tensors for export.
        sample_inputs_copy, sample_kwargs_copy = \
            _ortmodule_io.deepcopy_model_input(
                *inputs, **kwargs)

        # Ops behaving differently under train/eval mode need to exported with the
        # correct training flag to reflect the expected behavior.
        # For example, the Dropout node in a model is dropped under eval mode.
        assert self._export_mode is not None, "Please use a concrete instance of ExecutionManager"

        try:
            with torch.no_grad():
                torch.onnx.export(self._flattened_module,
                                  sample_inputs_copy + (sample_kwargs_copy, ),
                                  f,
                                  input_names=input_names,
                                  output_names=output_names,
                                  opset_version=ONNX_OPSET_VERSION,
                                  do_constant_folding=False,
                                  training=self._export_mode,
                                  dynamic_axes=dynamic_axes,
                                  verbose=self._verbosity < _ortmodule_utils.Verbosity.WARNING,
                                  export_params=False,
                                  keep_initializers_as_inputs=True)
        except RuntimeError as e:
            raise RuntimeError(
                'There was an error while exporting the PyTorch model to ONNX: {}'.format(e))

        return onnx.load_model_from_string(f.getvalue())

    def _set_device_from_module(self):
        """Get the device from the module and save it to self._device"""

        device_from_module = _utils.get_device_from_module(
            self._original_module)
        if not self._device or self._device != device_from_module:
            self._device = device_from_module
            if not self._device:
                raise RuntimeError(
                    'A device must be specified in the model or data!')

    def _convert_training_graph_input_to_list(self, *inputs, **kwargs):
        '''Creates forward `*inputs` list from user input and PyTorch initializers

        TODO: How IO binding model inputs and outputs affects initializer copies?

        ONNX Runtime forward requires an ordered list of:
            * User input: computed from forward InferenceSession
            * Initializers: computed from original PyTorch model parameters
        '''
        # User inputs
        non_none_inputs = [inp for inp in inputs if inp is not None]
        named_buffers_iter = iter(self._flattened_module.named_buffers())
        result = []
        for input_idx, name in enumerate(self._graph_info.user_input_names):
            inp = None
            if input_idx < len(non_none_inputs):
                inp = non_none_inputs[input_idx]
            elif name in kwargs and kwargs[name] is not None:
                inp = kwargs[name]
            elif input_idx >= len(non_none_inputs):
                # Registered buffers are translated to user_input+initializer in ONNX
                # TODO: Check what happens when the number of inputs change form one call to the next
                buffer_name, inp = next(named_buffers_iter)
                assert buffer_name == name, f'Input name {name} expected, but {buffer_name} found!'

            if inp is not None:
                result.append(inp)
            else:
                # TODO: Re-export ONNX if any input from onnx_graphs_info.user_input_names is None.
                raise RuntimeError(
                    f'Input is present in ONNX graph but not provided: {name}.')

        # Initializers
        for param in self._flattened_module.named_parameters():
            result.append(param[1])

        return result

    def _initialize_graph_builder(self, training):
        """Creates a new OrtModuleGraphBuilder, initializes it and saves it to self._graph_builder"""

        # TODO: PyTorch exporter bug: changes the initializer order in ONNX model
        initializer_names = [name
                             for name, _ in self._flattened_module.named_parameters()]
        initializer_names_to_train = [name
            for name, param in self._flattened_module.named_parameters() if param.requires_grad]

        # Build and optimize the full graph
        grad_builder_config = C.OrtModuleGraphBuilderConfiguration()
        grad_builder_config.initializer_names = initializer_names
        grad_builder_config.initializer_names_to_train = initializer_names_to_train
        grad_builder_config.input_names_require_grad = self._input_names_require_grad
        grad_builder_config.build_gradient_graph = training
        self._graph_builder = C.OrtModuleGraphBuilder()
        self._graph_builder.initialize(
            self._onnx_model.SerializeToString(), grad_builder_config)