import onnx
import torch
from inspect import signature

from . import ORTTrainerOptions
from . import optim
from .model_desc_validation import _ORTTrainerModelDesc

class TrainStepInfo(object):
    r"""Private class used to store runtime information from current train step.

    After every train step, :py:meth:`ORTTrainer.train_step` updates the internal instance of
    :py:class:`.TrainStepInfo` residing on :py:class:`.ORTTrainer` with relevant information
    from the forward pass.

    This class shouldn't be accessed directly by the user, unless they really know what they are doing.
    Instead, :py:class:`.ORTTrainer` passes it to relevant class methods automatically,
    such as :py:method:`._LRScheduler.get_lr` or :py:class:`.LossScaler.update`.

    Args:
        all_finite (bool): flag that indicates whether all gradients are still finite after last step
        step (int): indicates current step
        optimizer_config (optim._OptimizerConfig): reference to optimizer config

    Example:

        .. code-block:: python

            info = TrainStepInfo(all_finite=True, step=0, optimizer_config=optim.SGDConfig(lr=0.01))
            if info.all_finite:
                print(f'Yay, all gradients are finite at {step} step!')

    """

    def __init__(self, all_finite=None, step=None, optimizer_config=None):
        assert all_finite is None or isinstance(all_finite, bool),\
            "all_finite must be either None or a bool"
        assert step is None or (isinstance(step, int) and step >= 0),\
            "step must be either None or a positive int"
        assert optimizer_config is None or isinstance(optimizer_config, optim._OptimizerConfig),\
            "optimizer_config must be either None or optim._OptimizerConfig"

        self.all_finite = all_finite
        self.step = step
        self.optimizer_config = optimizer_config


class ORTTrainer(object):
    r"""Pytorch frontend for ONNX Runtime training

    Entry point that exposes the C++ backend of ORT as a Pytorch frontend.

    Args:
        model (torch.nn.Module or onnx.ModelProto): either a PyTorch or ONNX model.
            When a PyTorch model and :py:attr:`loss_fn` are specified, :py:attr:`model` and :py:obj:`loss_fn` are combined.
            When a ONNX model is provided, the loss is identified by the flag :py:obj:`is_loss=True` in one of the :py:attr:`.model_desc.outputs` entries.
        model_desc (dict): model input and output description.
            This is used to identify inputs and outputs and their shapes, so that ORT can generate back propagation graph, plan memory allocation for
            training, and perform optimizations.
            :py:attr:`model_desc` must be consistent with the training :py:attr:`model` and have the following (:py:obj:`dict`) schema
            :py:obj:`{ 'inputs': [tuple(name, shape)], 'outputs': [tuple(name, shape, is_loss)]}`.
            :py:attr:`name` is a string representing the name of input or output of the model.
            For :py:obj:`model_desc['inputs']` entries, :py:attr:`name` must match input names of the original PyTorch model's :py:meth:`torch.nn.Module.forward` method.
            For ONNX models, both name and order of input names must match.
            For :py:obj:`model_desc['outputs']` entries, the order must match the original PyTorch's output as returned by :py:meth:`torch.nn.Module.forward` method.
            For ONNX models, both name and order of output names must match.
            :py:attr:`shape` is a list of string or integers that describes the shape of the input/output.
            Each dimension size can be either a string or an int. String means the dimension size is dynamic, while integers mean static dimensions.
            An empty list implies a scalar.
            Lastly, :py:attr:`is_loss` is a boolean (default is False) that flags if this output is considered a loss.
            ORT backend needs to know which output is loss in order to generate back propagation graph.
            Loss output must be specified when either :py:attr:`loss_fn` is specified or when loss is embedded in the model.
            Note that only one loss output is supported per model.
        optimizer_config (optim._OptimizerConfig): optimizer config.
            One of :py:class:`.optim.AdamConfig`, :py:class:`.optim.LambConfig` or :py:class:`.optim.SGDConfig`.
        loss_fn (callable, default is None): a PyTorch loss function.
            It takes two inputs [prediction, label] and outputs a scalar loss tensor.
            If provided, :py:attr:`loss_fn` is combined with the PyTorch :py:attr:`model` to form a combined PyTorch model.
            Inputs to the combined PyTorch model are concatenation of the :py:attr:`model`'s input and :py:attr:`loss_fn`'s label input.
            Outputs of the combined PyTorch model are concatenation of :py:attr:`loss_fn`'s loss output and :py:attr:`model`'s outputs.
        options (ORTTrainerOptions, default is None): options for additional features.

    Example:

        .. code-block:: python

            model = ...
            loss_fn = ...
            model_desc = {
                "inputs": [
                    ("input_ids", ["batch", "max_seq_len_in_batch"]),
                    ("attention_mask", ["batch", "max_seq_len_in_batch"]),
                    ("token_type_ids", ["batch", "max_seq_len_in_batch"]),
                    ("masked_lm_labels", ["batch", "max_seq_len_in_batch"]),
                    ("next_sentence_label", ["batch", 1])
                ],
                "outputs": [
                    ("loss", [], True),
                ],
            }
            optim_config = optim.LambConfig(param_groups = [ { 'params' : ['model_param0'], 'alpha' : 0.8, 'beta' : 0.7},
                                                             { 'params' : ['model_param1' , 'model_param_2'], 'alpha' : 0.0}
                                                           ],
                                            alpha=0.9, beta=0.999)
            ort_trainer = ORTTrainer(model, model_desc, optim_config, loss_fn)
    """

    def __init__(self, model, model_desc, optim_config, loss_fn=None, options=None):
        # Basic validation
        assert model is not None, "'model' is required and must be either a 'torch.nn.Module' or ONNX model"
        assert isinstance(model_desc, dict), "'model_desc' must be a 'dict'"
        assert isinstance(optim_config, optim._OptimizerConfig),\
            "'optim_config' is required and must be any of 'AdamConfig', 'LambConfig' or 'SGDConfig'"
        assert loss_fn is None or (callable(loss_fn) and len(signature(loss_fn).parameters) == 2),\
            "'loss_fn' must be either 'None' or a callable with two parameters"
        assert options is None or isinstance(options, ORTTrainerOptions),\
            "'loss_fn' must be either 'None' or 'ORTTrainerOptions'"

        #            Model + Loss validation
        #           Supported combinarios are
        #    ----------------------------------------
        #   |   | Model            | Loss            |
        #    ----------------------------------------
        #   | 1 | torch.nn.Module  | None            |
        #   | 2 | torch.nn.Module  | torch.nn.Module |
        #   | 3 | ONNX             | None            |
        #    ----------------------------------------
        self._torch_model = None
        self._onnx_model = None
        if isinstance(model, torch.nn.Module):
            assert loss_fn is None or isinstance(model, torch.nn.Module),\
                "'loss_fn' must be either 'None' or 'torch.nn.Module'"
            self._torch_model = model
            self._loss_fn = loss_fn
        elif isinstance(model, onnx.ModelProto):
            assert loss_fn is None, "'loss_fn' must not be specified when 'model' is an ONNX model"
            self._onnx_model = model
            self._loss_fn = None
        else:
            raise ValueError("'model' must be either 'torch.nn.Module' or 'onnx.ModelProto'")

        self.model_desc = _ORTTrainerModelDesc(model_desc)
        self.optim_config = optim_config
        self.options = ORTTrainerOptions(options)

    def eval_step(self, *input, **kwargs):
        r"""Evaluation step method

        Args:
            *input: Arbitrary arguments that are used as model input (data only)
            **kwargs: Arbitrary keyword arguments that are used as model input (data only)

        Returns:
            ordered :py:obj:`list` with model outputs as described by :py:attr:`.ORTTrainer.model_desc`
        """
        pass

    def save_as_onnx(self, path):
        r"""Persists ONNX model into :py:attr:`path`

        The model will be saved as a Google Protocol Buffers (aka protobuf) file as per ONNX standard containing
        the full graph, including inference and training metadata.

        Args:
            path (str): Full path, including filename, to save the model in the filesystem
        """
        pass

    def convert_model_loss_fn_to_onnx(model, loss_fn, model_desc, device, inputs, opset_version=DEFAULT_OPSET_VERSION, _enable_internal_postprocess=True):
        # example: {input0:{0:'batch'}, input1:{0:'batch'}}
        dynamic_axes = {}
        for input in model_desc.inputs_:
            symbolic_axis = {}
            for i, axis in enumerate(input.shape_):
                if isinstance(axis, str):
                    symbolic_axis[i] = axis
            if len(symbolic_axis):
                dynamic_axes[input.name_] = symbolic_axis

        for output in model_desc.outputs_:
            symbolic_axis = {}
            for i, axis in enumerate(output.shape_):
                if isinstance(axis, str):
                    symbolic_axis[i] = axis
            if len(symbolic_axis):
                dynamic_axes[output.name_] = symbolic_axis

        input_names = [input.name_ for input in model_desc.inputs_]
        output_names = [output.name_ for output in model_desc.outputs_]

        if isinstance(inputs, torch.Tensor):
            inputs = [inputs]
        if isinstance(inputs, dict):
            sample_inputs = [inputs[k.name_].to(device=device) for k in model_desc.inputs_]
        elif isinstance(inputs, (list, tuple)):
            sample_inputs = [input.to(device=device) for i, input in enumerate(inputs) if i < len(model_desc.inputs_)]
        else:
            raise RuntimeError("Unexpected input type. Only torch.Tensor, or dict/list/tuple of torch.Tensor is supported.")

        # pytorch onnx exporter/trace does not try to match argument names.
        # e.g. for models with optional inputs, it requires all inputs be present.
        # this is a problem because the model graph depends on inputs provided.
        model = wrap_for_input_match(model, loss_fn, input_names)

        model.eval()
        with torch.no_grad():
            sample_outputs = model(*sample_inputs)
        if isinstance(sample_outputs, torch.Tensor):
            sample_outputs = [sample_outputs]
        for sample_output, output_desc in zip(sample_outputs, model_desc.outputs_):
            output_desc.dtype_ = sample_output.dtype
        model.train()

        f = io.BytesIO()

        # Other export options to use(this is for backward compatibility).
        other_export_options = {}
        other_export_options['training'] = True

        # This option was added after 1.4 release.
        if LooseVersion(torch.__version__) > LooseVersion('1.4.0'):
            other_export_options['enable_onnx_checker'] = False
        # This option was added after 1.6 release.
        if LooseVersion(torch.__version__) >= LooseVersion('1.6.0'):
            other_export_options['training'] = torch.onnx.TrainingMode.TRAINING

        torch.onnx._export(model, tuple(sample_inputs), f,
                        input_names=input_names,
                        output_names=output_names,
                        opset_version=opset_version,
                        dynamic_axes=dynamic_axes,
                        _retain_param_name=True,
                        example_outputs=tuple(sample_outputs),
                        do_constant_folding=False,
                        **other_export_options)

        onnx_model = onnx.load_model_from_string(f.getvalue())

        # Remove 'model_.' prefix introduced by model wrapper for initializers.
        replace_name_dict = {}
        for n in onnx_model.graph.initializer:
            if n.name.startswith('model_.'):
                replace_name_dict[n.name] = n.name[len('model_.'):]
                n.name = replace_name_dict[n.name]
        for n in onnx_model.graph.node:
            for i, name in enumerate(n.input):
                if name in replace_name_dict:
                    n.input[i] = replace_name_dict[name]

        # onnx model initializer may contain non-trainable registered buffers that are not part
        # of pytorch model named parameteres.
        named_parameters = model.model_.named_parameters() if hasattr(model, 'model_') else model.named_parameters()
        assert set([n for n, t in named_parameters]).issubset(
            set([n.name for n in onnx_model.graph.initializer])), \
            "Initializer names do not match between PyTorch model and ONNX model, " \
            "please report a bug to ONNX Runtime."

        if _enable_internal_postprocess:
            onnx_model = postprocess.run_postprocess(onnx_model)

        return onnx_model

    def _init_onnx_model_(self, *input):
        if self._onnx_model is not None:
            return

        if self._torch_model is not None:
            self.torch_model_.cpu()
            # convert the model
            # get input, outputs, export model
            self.onnx_model = self.convert_model_loss_fn_to_onnx(self._torch_model, self.loss_fn, self.model_desc, torch.device('cpu'), inputs, opset_version=self.opset_version, _enable_internal_postprocess=self._enable_internal_postprocess)
            
            # selected tasks from init_sesion
            if self._enable_internal_postprocess:
                self._onnx_model_ = postprocess.run_postprocess(self.onnx_model_)

            if self._extra_postprocess:
                self._extra_postprocess(self.onnx_model_)

            self._verify_fully_optimized_model(self.onnx_model_)
        

    def train_step(self, *input, **kwargs):
        r"""Train step method

        After forward pass, an ordered list with all outputs described at :py:attr:`ORTTrainer.model_desc` is returned.
        Additional information relevant to the train step is maintend by :py:attr:`ORTTrainer._train_step_info`.
        See :py:class:`.TrainStepInfo` for details.

        Args:
            *input: Arbitrary arguments that are used as model input (data only)
            **kwargs: Arbitrary keyword arguments that are used as model input (data only)

        Returns:
            ordered :py:obj:`list` with model outputs as described by :py:attr:`ORTTrainer.model_desc`
        """
        if self._onnx_model is None:
            self._init_onnx_model_(*input) 
