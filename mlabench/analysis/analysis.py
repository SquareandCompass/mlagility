import sys
import os
import argparse
import inspect
import importlib.util
import copy
import time
import shlex
import functools
import dataclasses
import pathlib
from typing import Union, List, Dict
from types import FrameType, TracebackType
import torch
import transformers
import tensorflow as tf
import mlabench.analysis.status as status
import mlabench.analysis.util as util
from mlabench.analysis.util import ModelInfo
import mlabench.benchmark.cloud as cloud
from groqflow.common import printing
import groqflow.common.build as build
import groqflow.common.exceptions as exp
from groqflow import groqit
import groqflow


@dataclasses.dataclass
class TracerArgs:
    input: str
    labels: List[str]
    lean_cache: bool
    targets: List[str]
    check_ops: bool
    build: bool
    max_depth: int
    cache_dir: str
    rebuild: str
    compiler_flags: List[str]
    assembler_flags: List[str]
    num_chips: int
    groqview: bool
    models_found: Dict[str, ModelInfo] = dataclasses.field(default_factory=dict)
    script_name: str = None
    benchmark_gpu: bool = False
    benchmark_cpu: bool = False

    @functools.cached_property
    def torch_activations(self) -> List[str]:
        act = util.get_classes(torch.nn.modules.activation)
        if "activations" in dir(transformers):
            act += util.get_classes(transformers.activations)
        return act


def call_groqit(
    model_inputs: dict, model_info: ModelInfo, tracer_args: TracerArgs
) -> None:
    """
    Calls the groqit function from within the model forward function
    """

    # Update status to "computing"
    model_info.status_message = "Computing..."
    model_info.status_message_color = printing.Colors.OKBLUE
    status.update(tracer_args.models_found, tracer_args.script_name)

    # Get a copy of the keyword arguments
    args, kwargs = model_inputs
    inputs = {}
    for k in kwargs.keys():
        if torch.is_tensor(kwargs[k]):
            inputs[k] = torch.tensor(kwargs[k].detach().numpy())
        else:
            inputs[k] = copy.deepcopy(kwargs[k])

    # Convert all positional arguments into keyword arguments
    if args != ():
        if model_info.model_type == build.ModelType.PYTORCH:
            forward_function = model_info.model.forward
        elif model_info.model_type == build.ModelType.KERAS:
            forward_function = model_info.model.call
        all_args = list(inspect.signature(forward_function).parameters.keys())
        for i in range(len(args)):
            if torch.is_tensor(args[i]):
                inputs[all_args[i]] = torch.tensor(args[i].detach().numpy())
            else:
                inputs[all_args[i]] = args[i]
    model_info.inputs = inputs

    # Prepare other groqit parameters
    if model_info.check_ops:
        if model_info.model_type == build.ModelType.PYTORCH:
            seq = util.check_ops_pytorch
        elif model_info.model_type == build.ModelType.KERAS:
            seq = util.check_ops_keras
    else:
        seq = None

    cache_dir = (
        build.DEFAULT_CACHE_DIR
        if tracer_args.cache_dir is None
        else tracer_args.cache_dir
    )
    build_name = f"{tracer_args.script_name}_{model_info.hash}"

    # Save model labels
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)
    labels_dir = os.path.join(cache_dir, "labels")
    if not os.path.exists(labels_dir):
        os.makedirs(labels_dir)
    labels = [] if tracer_args.labels is None else tracer_args.labels
    labels = labels + [f"class::{type(model_info.model).__name__}"]
    with open(
        os.path.join(labels_dir, f"{build_name}.txt"),
        "w",
        encoding="utf8",
    ) as fp:
        fp.write(" ".join(labels))

    try:
        groqit(
            model_info.model,
            inputs,
            sequence=seq,
            build_name=build_name,
            monitor=True,
            rebuild=tracer_args.rebuild,
            cache_dir=cache_dir,
            compiler_flags=tracer_args.compiler_flags,
            assembler_flags=tracer_args.assembler_flags,
            num_chips=tracer_args.num_chips,
            groqview=tracer_args.groqview,
        )
        if model_info.check_ops:
            model_info.status_message = "All ops supported!"
        else:
            model_info.status_message = "Model successfully built!"
        model_info.status_message_color = printing.Colors.OKGREEN

    except groqflow.common.exceptions.GroqitStageError:
        load_state = build.load_state(build_name=build_name)
        if len(load_state.info.opt_onnx_unsupported_ops) > 0:
            model_info.status_message = "Unsupported op(s) " + ", ".join(
                load_state.info.opt_onnx_unsupported_ops
            )
        else:
            model_info.status_message = (
                "GroqIt Stage Errror: see log files for details."
            )
        model_info.status_message_color = printing.Colors.WARNING

    except groqflow.common.exceptions.GroqFlowError:
        model_info.status_message = "GroqFlowError: see log files for details."
        model_info.status_message_color = printing.Colors.WARNING

    # This broad exception is ok since enumerating all exceptions is
    # not possible, as the tested software continuously evolves.
    except Exception as e:  # pylint: disable=broad-except
        util.stop_stdout_forward()
        model_info.status_message = f"Unknown GroqIt error: {e}"
        model_info.status_message_color = printing.Colors.WARNING

    # Execute the models found on GPU for competitive benchmarking
    if tracer_args.benchmark_gpu:
        load_state = build.load_state(cache_dir=cache_dir, build_name=build_name)
        if not load_state.info.opt_onnx_exported:
            raise exp.GroqFlowError(
                "GPU benchmarking failed because ONNX file was not created for this model"
            )
        log_execute_path = os.path.join(
            build.output_dir(load_state.cache_dir, load_state.config.build_name),
            "log_gpu_execute.txt",
        )
        # Due to tail latency issues, gpus runs are executed for 100 iterations and averaged
        gpu_iterations = 100
        cloud.execute_gpu_remotely(load_state, log_execute_path, gpu_iterations)

    # Execute the models found on CPU for competitive benchmarking
    if tracer_args.benchmark_cpu:
        load_state = build.load_state(cache_dir=cache_dir, build_name=build_name)
        if not load_state.info.opt_onnx_exported:
            raise exp.GroqFlowError(
                "CPU benchmarking failed because ONNX file was not created for this model"
            )
        log_execute_path = os.path.join(
            build.output_dir(load_state.cache_dir, load_state.config.build_name),
            "log_cpu_execute.txt",
        )
        # Due to tail latency issues, cpus runs are executed for 100 iterations and averaged
        cpu_iterations = 100
        cloud.execute_cpu_remotely(load_state, log_execute_path, cpu_iterations)

    # Add metadata and clean cache if needed
    output_dir = os.path.join(cache_dir, build_name)
    if os.path.isdir(output_dir):
        # Delete all files except logs and other metadata
        if tracer_args.lean_cache:
            util.clean_output_dir(output_dir)


def get_model_hash(
    model: Union[torch.nn.Module, tf.keras.Model], model_type: build.ModelType
):
    return build.hash_model(model, model_type, hash_params=False)[:8]


def store_model_info(
    model: Union[torch.nn.Module, tf.keras.Model],
    model_name: str,
    model_type: build.ModelType,
    frame: FrameType,
    event: str,
    tracer_args: TracerArgs,
    depth: int,
    parent_hash: str,
):
    # Getting the model hash is only possible after the first inference of Keras models
    model_hash = get_model_hash(model, model_type)

    # File where the model was found
    file = str(frame)[str(frame).find("file ") + 6 : str(frame).find("',")]

    # Line where the model was found
    line = frame.f_lineno if event == "return" else frame.f_lineno - 1

    # Keep track of all models details
    if model_hash not in tracer_args.models_found.keys():
        tracer_args.models_found[model_hash] = ModelInfo(
            model=model,
            name=model_name,
            file=file,
            line=line,
            depth=depth,
            hash=model_hash,
            parent_hash=parent_hash,
            is_target=model_hash in tracer_args.targets or tracer_args.targets == [],
            check_ops=tracer_args.check_ops,
            build_model=tracer_args.build,
            model_type=model_type,
        )


def explore_frame(
    frame,
    event,
    local_var_name,
    local_var,
    tracer_args: TracerArgs,
    depth: int = 0,
    parent_hash: Union[str, None] = None,
):
    """
    This function checks whether local_var is a torch or keras model.
    If it is, we will modify its forward function to know when it
    is called.
    """

    # Skip all variables that are not a subclass of torch.nn.Module/tf.keras.Model
    # Note: try block used since dead weakreferences fail when checking subclass
    try:
        if issubclass(type(local_var), torch.nn.Module):
            if type(local_var) in tracer_args.torch_activations:
                return
            model_type = build.ModelType.PYTORCH
        elif issubclass(type(local_var), tf.keras.Model):
            model_type = build.ModelType.KERAS
        else:
            return
    except AttributeError:
        return

    # Skip self variable and variable names commonly used by child models
    if (
        local_var_name == "self"
        or local_var_name == "instance"
        or local_var_name == "child"
        or local_var_name == "layer"
        or local_var_name == "module"
    ):
        return

    # Check if we are inside of a subclass of torch.nn.Module or tf.keras.model
    inside_class = False
    inside_nn_subclass = False
    if "self" in frame.f_locals:
        self_var = frame.f_locals["self"]
        inside_class = type(self_var)
        inside_nn_subclass = issubclass(inside_class, (torch.nn.Module, tf.keras.Model))

    if not hasattr(local_var, "forward_instrumented") and not inside_nn_subclass:

        if model_type == build.ModelType.PYTORCH:

            # Avoid instrumenting models before they have been fully loaded
            if util.count_parameters(local_var, model_type) == 0:
                return

            # Mark this model as instrumented
            local_var.forward_instrumented = True

            # Create a copy of the old forward function
            old_forward = local_var.forward

            # Recursively look for sub-models within the found model
            # This is only possible on Pytorch, since each layer of a torch.nn.module
            # is also a torch.nn.module.
            model_hash = get_model_hash(local_var, model_type)
            if depth < tracer_args.max_depth:
                recursive_search(
                    frame, event, local_var, depth, model_hash, tracer_args
                )

            # We can keep track of Pytorch models even before they are executed
            store_model_info(
                local_var,
                local_var_name,
                model_type,
                frame,
                event,
                tracer_args,
                depth,
                parent_hash,
            )
        elif model_type == build.ModelType.KERAS:
            # Mark this model as instrumented
            local_var.forward_instrumented = True

            # Create a copy of the old forward function
            old_forward = local_var.call

            # Raise exception if user tries to use max_depth!=0 for a keras model
            if tracer_args.max_depth != 0:
                raise exp.GroqFlowError("max_depth is not supported for Keras models")
        local_var.old_forward = old_forward

        def forward_spy(*args, **kwargs):

            tracer = sys.getprofile()
            if tracer is not None:
                # Turn tracing off while the model is being executed for speed
                sys.setprofile(None)
            elif depth == 0:
                # If this model is being executed and the tracing is already off
                # we are calling a module within a parent module. We only run
                # groqit on child models if the user has explicitly asked us to
                # do so by setting the max_depth flag.
                return old_forward(*args, **kwargs)

            # Keep track of execution time
            start_time = time.time()
            outputs = old_forward(*args, **kwargs)
            end_time = time.time()

            # We can only keep track of keras models once they have been executed
            if model_type == build.ModelType.KERAS:
                store_model_info(
                    local_var,
                    local_var_name,
                    model_type,
                    frame,
                    event,
                    tracer_args,
                    depth,
                    parent_hash,
                )
            model_hash = get_model_hash(local_var, model_type)
            model_info = tracer_args.models_found[model_hash]
            model_info.exec_time = model_info.exec_time + end_time - start_time

            model_info.executed = model_info.executed + 1

            # Call groqit if this is the first time the model is being executed
            # and this model has been selected by the user
            if (
                model_info.executed == 1
                and model_info.is_target
                and (model_info.check_ops or model_info.build_model)
            ):
                call_groqit(
                    model_inputs=[args, kwargs],
                    model_info=model_info,
                    tracer_args=tracer_args,
                )
                # Ensure that groqit() doesn't interfere with our execution count
                model_info.executed = 1

            status.update(tracer_args.models_found, tracer_args.script_name)

            # Turn tracing on again after computing the outputs
            sys.setprofile(tracer)

            return outputs

        # The inspect module offers the ability to actually copy the signature of the wrapped
        # function. This allows other functions to see the correct parameters instead of the
        # enigmatic *args, **kwargs. This is especially important for Keras, since it heavily
        # relies on inspections to the call function.
        forward_spy.__signature__ = inspect.signature(old_forward)

        # Use modified forward/call function
        if model_type == build.ModelType.PYTORCH:
            local_var.forward = forward_spy
        elif model_type == build.ModelType.KERAS:
            local_var.call = forward_spy


def tracefunc(
    frame: FrameType, event: str, _, tracer_args: TracerArgs
) -> TracebackType:
    """
    This function is used to trace the program as it runs in order
    to keep track of all all instantiated models.
    This function is passed to sys.setprofile() as a callback function.
    It receives three arguments:
        frame (the stack frame from the code being run),
        event (a string naming the type of notification), and
        arg (an event-specific value)

    """

    # Create a copy of f_locals.keys() to avoid errors due to dict changing
    local_names = list(frame.f_locals.keys())

    # Loop over all local variables to check if new models can be found
    for local_var_name in local_names:
        explore_frame(
            frame,
            event,
            local_var_name,
            frame.f_locals[local_var_name],
            tracer_args=tracer_args,
            depth=0,
        )

    return tracefunc


def recursive_search(
    frame: FrameType,
    event: str,
    model: Union[torch.nn.Module, tf.keras.Model],
    depth: int,
    parent_hash: Union[str, None],
    tracer_args: TracerArgs,
):
    """
    Recursively check for submodels within found models
    """
    element_names = list(dict(model.named_modules()).keys())[1:]
    for element_name in element_names:
        if hasattr(model, element_name):
            element = getattr(model, element_name)
            if issubclass(type(element), torch.nn.Module):
                explore_frame(
                    frame,
                    event,
                    element_name,
                    element,
                    tracer_args,
                    depth=depth + 1,
                    parent_hash=parent_hash,
                )


def main():

    # Parse args
    parser = argparse.ArgumentParser()

    # Autogroq lists all models instantiated within a given python script and
    # calls groqit() on the subset of those models that have been executed
    # according to the arguments described below.

    # The first positional argument must be a Groq-agnostic Python file
    # You must be in the same directory as the Python file to run this script
    parser.add_argument("input", help="Input .py file")

    # The user may perform one of the following operations
    #   check_ops:  Checks whether all operations of the found models are supported
    #               by Groq's compiler.
    #   build:      Checks whether all operations of the found models are supported
    #               by Groq's compiler, and attempts to build the found models
    #               using groqit().
    #
    # Notes:
    #   * If none of the above arguments are provided, autogroq will simply list
    #     all models that have been found within the script. This list includes
    #     information such as the name, hash, and size of each model.
    #   * Those operations will target all models found within the script unless
    #     specific models are selected. The user may select specific models by
    #     providing the hashes of the models to be targeted.
    #     Example: autogroq input.py --build a76awg8w
    #   * Models must be called at least once in the input script.
    mutually_exclusive = parser.add_mutually_exclusive_group()
    mutually_exclusive.add_argument("--check-ops", type=str, nargs="*")
    mutually_exclusive.add_argument("--build", type=str, nargs="*")

    # max_depth is used to look for models instantiated within models
    # This might be useful to analyze which sub-components of a lager
    # model are supported by groqit().
    parser.add_argument("-d", "--max-depth", type=int, default=0)

    # The user may save additional metadata to each built model through the
    # labels argument. This metadata can then be later consumed by other
    # tools to do things such as filtering models by author or task.
    parser.add_argument("--labels", type=str, nargs="+")

    # Path to GroqFlow's cache
    parser.add_argument("--cache-dir", type=str)

    # Arguments of the input script (all together as a string)
    parser.add_argument("--input-args", type=str, nargs=1)

    # Argument to enable competitive GPU benchmarking
    parser.add_argument("--benchmark-gpu", action="store_true")
    parser.set_defaults(benchmark_gpu=False)

    # Argument to enable competitive CPU benchmarking
    parser.add_argument("--benchmark-cpu", action="store_true")
    parser.set_defaults(benchmark_cpu=False)

    # Deletes all files generated by groqit() except logs and metadata
    # This is used to keep disk utilization low while also keeping track
    # of groqit's ability to build a given model.
    parser.add_argument("--lean-cache", action="store_true")
    parser.set_defaults(lean_cache=False)

    autogroq_args = parser.parse_args()

    # Pack the results of check_ops and build in the targets variable
    # This is used to simplify autogroq's code
    if autogroq_args.check_ops is not None:
        setattr(autogroq_args, "targets", autogroq_args.check_ops)
    elif autogroq_args.build is not None:
        setattr(autogroq_args, "targets", autogroq_args.build)
    else:
        setattr(autogroq_args, "targets", [])

    # Convert check_ops and build to booleans
    autogroq_args.check_ops = autogroq_args.check_ops is not None
    autogroq_args.build = autogroq_args.build is not None

    tracer_args = TracerArgs(
        input=autogroq_args.input,
        labels=autogroq_args.labels,
        lean_cache=autogroq_args.lean_cache,
        benchmark_gpu=autogroq_args.benchmark_gpu,
        benchmark_cpu=autogroq_args.benchmark_cpu,
        targets=autogroq_args.targets,
        check_ops=autogroq_args.check_ops,
        build=autogroq_args.build,
        max_depth=autogroq_args.max_depth,
        cache_dir=autogroq_args.cache_dir,
        rebuild="always",
        compiler_flags=None,
        assembler_flags=None,
        num_chips=None,
        groqview=False,
    )

    evaluate_script(
        tracer_args=tracer_args,
        input_args=autogroq_args.input_args,
    )


def evaluate_script(tracer_args: TracerArgs, input_args: str = None):
    # Trim the ".py"
    tracer_args.script_name = pathlib.Path(tracer_args.input).stem

    # Get a pointer to the script's python module
    spec = importlib.util.spec_from_file_location(
        tracer_args.script_name, tracer_args.input
    )
    module = importlib.util.module_from_spec(spec)

    # Overwriting argv to import input script using "input-args"
    if input_args is None:
        input_args = []
    else:
        input_args = shlex.split(input_args[0])
    sys.argv = [tracer_args.input] + input_args
    sys.path.append(os.getcwd())

    # Create a tracer object that bundles a callback function with some args
    tracer = functools.partial(tracefunc, tracer_args=tracer_args)

    # Enabling autogroq via setprofile
    sys.setprofile(tracer)

    # Import input script. Each executed frame of the input script will
    # trigger the tracefunc() callback function (defined above)
    spec.loader.exec_module(module)


if __name__ == "__main__":
    main()
