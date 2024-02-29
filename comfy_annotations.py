import functools
import inspect
import logging

import inspect
from typing import Callable, get_args, get_origin
import torch
import logging
import inspect
from enum import Enum

default_category = "ComfyFunc"

class AutoDescriptionMode(Enum):
    NONE = "none"
    BRIEF = "brief"
    FULL = "full"

# Whether to automatically use the docstring as the description for nodes.
# If set to AutoDescriptionMode.FULL, the full docstring will be used, whereas
# AutoDescriptionMode.BRIEF will use only the first line of the docstring.
docstring_mode = AutoDescriptionMode.FULL


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}


# Use as a default str value to show choices to the user.
class Choice(str):
    def __new__(cls, choices: list[str]):
        instance = super().__new__(cls, choices[0])
        instance.choices = choices
        return instance

    def __str__(self):
        return self.choices[0]


class StringInput(str):
    def __new__(cls, value, multiline=False):
        instance = super().__new__(cls, value)
        instance.multiline = multiline
        return instance

    def to_dict(self):
        return {"default": self, "multiline": self.multiline}


class NumberInput(float):
    def __new__(
        cls,
        default,
        min=None,
        max=None,
        step=None,
        round=None,
        display: str = "number",
    ):
        if min is not None and default < min:
            raise ValueError(
                f"Value {default} is less than the minimum allowed {min}."
            )
        if max is not None and default > max:
            raise ValueError(
                f"Value {default} is greater than the maximum allowed {max}."
            )
        instance = super().__new__(cls, default)
        instance.min = min
        instance.max = max
        instance.display = display
        instance.step = step
        instance.round = round
        return instance

    def to_dict(self):
        metadata = {
            "default": self,
            "display": self.display,
            "min": self.min,
            "max": self.max,
            "step": self.step,
            "round": self.round,
        }
        metadata = {k: v for k, v in metadata.items() if v is not None}
        return metadata

    def __repr__(self):
        return f"{super().__repr__()} (Min: {self.min}, Max: {self.max})"


# Used for type hinting semantics only.
class ImageTensor(torch.Tensor):
    def __new__(cls):
        raise TypeError("Do not instantiate this class directly.")


# Used for type hinting semantics only.
class MaskTensor(torch.Tensor):
    def __new__(cls):
        raise TypeError("Do not instantiate this class directly.")


# Made to match any and all other types.
class AnyType(str):
    def __ne__(self, __value: object) -> bool:
        return False


any_type = AnyType("*")


_ANNOTATION_TO_COMFYUI_TYPE = {}
_SHOULD_AUTOCONVERT = {}

def get_fully_qualified_name(cls):
    return f"{cls.__module__}.{cls.__qualname__}"

def register_type(cls, name: str, should_autoconvert: bool = False):
    key = get_fully_qualified_name(cls)
    assert key not in _ANNOTATION_TO_COMFYUI_TYPE, f"Type {cls} already registered."
    _ANNOTATION_TO_COMFYUI_TYPE[key] = name    
    _SHOULD_AUTOCONVERT[key] = should_autoconvert

register_type(torch.Tensor, "IMAGE")
register_type(ImageTensor, "IMAGE")
register_type(MaskTensor, "MASK")
register_type(int, "INT")
register_type(float, "FLOAT")
register_type(str, "STRING")
register_type(bool, "BOOLEAN")
register_type(AnyType, any_type),


def get_type_str(the_type) -> str:
    key = get_fully_qualified_name(the_type)
    if key not in _ANNOTATION_TO_COMFYUI_TYPE and get_origin(the_type) is list:
        return get_type_str(get_args(the_type)[0])

    if key not in _ANNOTATION_TO_COMFYUI_TYPE and the_type is not inspect._empty:
        logging.warning(
            f"Type '{the_type}' not registered with ComfyUI, treating as wildcard"
        )

    type_str = _ANNOTATION_TO_COMFYUI_TYPE.get(key, any_type)
    return type_str


def get_device():
    return torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


def ComfyFunc(
    category: str = default_category,
    display_name: str = None,
    workflow_name: str = None,
    description: str = None,
    is_output_node: bool = False,
    return_types: list = None,
    return_names: list[str] = None,
    validate_inputs: Callable = None,
    is_changed: Callable = None,
    debug: bool = False,
):
    """
    Decorator function for creating ComfyUI nodes.

    Args:
        category (str): The category of the node.
        display_name (str): The display name of the node. If not provided, it will be generated from the function name.
        workflow_name (str): The workflow name of the node. If not provided, it will be generated from the function name.
        is_output_node (bool): Indicates whether the node is an output node and should be run regardless of if anything depends on it.
        return_types (list): A list of types to return. If not provided, it will be inferred from the function's annotations.
        return_names (list[str]): The names of the outputs. Must match the number of return types.
        validate_inputs (Callable): A function used to validate the inputs of the node.
        is_changed (Callable): A function used to determine if the node's inputs have changed.
        debug (bool): Indicates whether to enable debug logging for this node.

    Returns:
        A callable used that can be used with a function to create a ComfyUI node.
    """
    def decorator(func):
        wrapped_name = func.__qualname__ + "_comfyfunc_wrapper"
        if debug:
            logger = logging.getLogger(wrapped_name)
            logger.info(
                "-------------------------------------------------------------------"
            )
            logger.info(f"Decorating {func.__qualname__}")

        node_class = _get_node_class(func)

        is_static = _is_static_method(node_class, func.__name__)
        is_cls_mth = _is_class_method(node_class, func.__name__)
        is_member = node_class is not None and not is_static and not is_cls_mth

        required_inputs, optional_inputs, input_is_list_map, input_type_map = (
            _infer_input_types_from_annotations(func, is_member, debug)
        )

        if debug:
            logger.info(
                func.__name__, 
                "Is static:", is_static,
                "Is member:", is_member,
                "Class method:", is_cls_mth,
            )
            logger.info("Required inputs:", required_inputs)
            logger.info(required_inputs, optional_inputs, input_is_list_map)

        adjusted_return_types = []
        output_is_list = []
        if return_types is not None:
            adjusted_return_types, output_is_list = _infer_return_types_from_annotations(
                return_types, debug
            )
        else:
            adjusted_return_types, output_is_list = _infer_return_types_from_annotations(func, debug)

        if return_names:
            assert len(return_names) == len(
                adjusted_return_types
            ), f"Number of output names must match number of return types. Got {len(return_names)} names and {len(return_types)} return types."

        # There's not much point in a node that doesn't have any outputs
        # and isn't an output itself, so auto-promote in that case.
        force_output = len(adjusted_return_types) == 0
        name_parts = [x.title() for x in func.__name__.split("_")]
        input_is_list = any(input_is_list_map.values())

        def verify_image_tensor(arg, name=""):
            if isinstance(arg, torch.Tensor):
                assert len(arg.shape) in [3, 4], f"{wrapped_name}: Image tensor must have BxHxW or BxHxWxC shape, got {arg.shape}"
                if len(arg.shape) == 4:
                    assert arg.shape[3] in [1, 3, 4], f"{wrapped_name}: Image tensor must have 1, 3, or 4 channels, got {arg.shape[3]}"
                if arg.shape[0] > arg.shape[1] or arg.shape[0] > arg.shape[2]:
                    logging.warning(f"{wrapped_name}: Image tensor {arg.shape} has a larger B dimension than H or W")
                if arg.shape[1] > arg.shape[2] * 10 or arg.shape[2] > arg.shape[1] * 10:
                    logging.warning(f"{wrapped_name}: Image tensor {arg.shape} has a very large H or W dimension")
                
            elif isinstance(arg, list):
                for a in arg:
                    verify_image_tensor(a)
                    
        def verify_mask_tensor(arg, name=""):
            if isinstance(arg, torch.Tensor):
                assert len(arg.shape) == 3, f"{wrapped_name}: Mask tensor must have BxHxWxC shape, got {arg.shape}"
                if arg.shape[0] > arg.shape[1] or arg.shape[0] > arg.shape[2]:
                    logging.warning(f"{wrapped_name}: Image tensor {arg.shape} has a larger B dimension than H or W")
                if arg.shape[1] > arg.shape[2] * 10 or arg.shape[2] > arg.shape[1] * 10:
                    logging.warning(f"{wrapped_name}: Image tensor {arg.shape} has a very large H or W dimension")
            elif isinstance(arg, list):
                for a in arg:
                    verify_mask_tensor(a)
                    
        def all_to(device, arg):
            if isinstance(arg, torch.Tensor):
                return arg.to(device)
            elif isinstance(arg, list):
                return [all_to(device, a) for a in arg]
            return arg

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if debug:
                logger.info(
                    func.__name__,
                    "wrapper called with",
                    len(args),
                    "args and",
                    len(kwargs),
                    "kwargs. Is cls mth:",
                    is_cls_mth,
                )
                for i, arg in enumerate(args):
                    logger.info("arg", i, type(arg))
                for key, arg in kwargs.items():
                    logger.info("kwarg", key, type(arg))

            all_inputs = {**required_inputs, **optional_inputs}

            for key, arg in kwargs.items():
                kwargs[key] = all_to(get_device(), arg)
                
                if key in all_inputs:
                    the_type = get_fully_qualified_name(input_type_map[key])
                    if _SHOULD_AUTOCONVERT.get(the_type, False):
                        converted_input = input_type_map[key](arg)
                        kwargs[key] = converted_input

                # If it's supposed to be an image tensor, verify the dims
                if ((key in required_inputs and required_inputs[key][0] == "IMAGE") or 
                    (key in optional_inputs and optional_inputs[key][0] == "IMAGE")):
                    verify_image_tensor(arg)
                
                if ((key in required_inputs and required_inputs[key][0] == "MASK") or 
                    (key in optional_inputs and optional_inputs[key][0] == "MASK")):
                    verify_mask_tensor(arg)

            # For some reason self still gets passed with class methods.
            if is_cls_mth:
                args = args[1:]

            # If the python function didn't annotate it as a list,
            # but INPUT_TYPES does, then we need to convert make it not a list.
            if input_is_list:
                for arg_name in kwargs.keys():
                    if debug:
                        print("kwarg:", arg_name, len(kwargs[arg_name]))
                    if not input_is_list_map[arg_name]:
                        assert len(kwargs[arg_name]) == 1
                        kwargs[arg_name] = kwargs[arg_name][0]

            result = func(*args, **kwargs)
            
            num_expected_returns = len(adjusted_return_types)
            if num_expected_returns == 0:
                assert result is None, f"{wrapped_name}: Return value is not None, but no return type specified."
                return (None,)

            if not isinstance(result, tuple):
                result = (result,)                
            assert len(result) == len(adjusted_return_types), f"{wrapped_name}: Number of return values {len(new_result)} does not match number of return types {len(adjusted_return_types)}"

            for i, ret in enumerate(result):
                if ret is None:
                    logging.warning(f"Result {i} is None")

            new_result = all_to("cpu", list(result))
            for i, ret in enumerate(result):
                if debug:
                    logging.info(f"Result {i} is {type(ret)}")

                # Check dimensions of output tensors
                if adjusted_return_types[i] == "IMAGE":
                    verify_image_tensor(ret)
                if adjusted_return_types[i] == "MASK":
                    verify_mask_tensor(ret)
                        
            return tuple(new_result)

        if node_class is None or is_static:
            wrapper = staticmethod(wrapper)

        if is_cls_mth:
            wrapper = classmethod(wrapper)
            
        the_description = description
        if the_description is None and docstring_mode is not AutoDescriptionMode.NONE:
            if func.__doc__:
                the_description = func.__doc__.strip()
                if docstring_mode == AutoDescriptionMode.BRIEF:
                    the_description = the_description.split("\n")[0]

        _create_comfy_node(
            wrapped_name,
            category,
            node_class,
            wrapper,
            display_name if display_name else " ".join(name_parts),
            workflow_name if workflow_name else "".join(name_parts),
            required_inputs,
            optional_inputs,
            input_is_list,
            adjusted_return_types,
            return_names,
            output_is_list,
            description=the_description,
            is_output_node=is_output_node or force_output,
            validate_inputs=validate_inputs,
            is_changed=is_changed,
            debug=debug,
        )

        # Return the original function so it can still be used as normal (only ComfyUI sees the wrapper function).
        return func

    return decorator


def _annotate_input(
    type_name, default=inspect.Parameter.empty, debug=False
) -> tuple[tuple, bool]:
    has_default = default != inspect.Parameter.empty
    if type_name in ["INT", "FLOAT"]:
        default_value = 0
        if default != inspect.Parameter.empty:
            default_value = default
            if isinstance(default_value, NumberInput):
                return (type_name, default_value.to_dict()), False
        if debug:
            print(f"Default value for {type_name} is {default_value}")
        return (type_name, {"default": default_value, "display": "number"}), has_default
    elif type_name in ["STRING"]:
        default_value = default if default != inspect.Parameter.empty else ""
        if isinstance(default_value, Choice):
            return (default_value.choices,), False
        if isinstance(default_value, StringInput):
            return (type_name, default_value.to_dict()), False
        return (type_name, {"default": default_value}), has_default
    return (type_name,), has_default


def _infer_input_types_from_annotations(func, skip_first, debug=False):
    """
    Infer input types based on function annotations.
    """
    input_is_list = {}
    input_type_map = {}
    sig = inspect.signature(func)
    input_types = {}
    optional_input_types = {}

    params = list(sig.parameters.items())

    if debug:
        print("ALL PARAMS", params)

    if skip_first:
        if debug:
            print("SKIPPING FIRST PARAM ", params[0])
        params = params[1:]

    for param_name, param in params:
        origin = get_origin(param.annotation)
        input_is_list[param_name] = origin is list
        input_type_map[param_name] = param.annotation
        
        if debug:
            print("Param default:", param.default)
        comfyui_type = get_type_str(param.annotation)
        the_param, is_optional = _annotate_input(comfyui_type, param.default, debug)
        if not is_optional:
            input_types[param_name] = the_param
        else:
            optional_input_types[param_name] = the_param
    return input_types, optional_input_types, input_is_list, input_type_map


def _infer_return_types_from_annotations(func_or_types, debug=False):
    """
    Infer whether each element in a function's return tuple is a list or a single item,
    handling direct list inputs as well as function annotations.
    """
    if isinstance(func_or_types, list):
        # Direct list of types provided
        return_args = func_or_types
        origin = tuple  # Assume tuple if directly provided with a list
    else:
        # Assuming it's a function, inspect its return annotation
        return_annotation = inspect.signature(func_or_types).return_annotation
        return_args = get_args(return_annotation)
        origin = get_origin(return_annotation)

        if debug:
            print(f"return_annotation: '{return_annotation}'")
            print(f"return_args: '{return_args}'")
            print(f"origin: '{origin}'")
            print(type(return_annotation), return_annotation)

    types_mapped = []
    output_is_list = []

    if origin is tuple:
        for arg in return_args:
            if get_origin(arg) == list:
                output_is_list.append(True)
                list_arg = get_args(arg)[0]
                types_mapped.append(get_type_str(list_arg))
            else:
                output_is_list.append(False)
                types_mapped.append(get_type_str(arg))
    elif origin is list:
        if debug:
            print(get_type_str(return_annotation))
            print(return_annotation)
            print(return_args)
        types_mapped.append(get_type_str(return_args[0]))
        output_is_list.append(origin is list)
    elif return_annotation is not inspect.Parameter.empty:
        types_mapped.append(get_type_str(return_annotation))
        output_is_list.append(False)

    return_types_tuple = tuple(types_mapped)
    output_is_lists_tuple = tuple(output_is_list)
    if debug:
        print(f"return_types_tuple: '{return_types_tuple}', output_is_lists_tuple: '{output_is_lists_tuple}'")

    return return_types_tuple, output_is_lists_tuple


def _create_comfy_node(
    cname,
    category,
    node_class,
    process_function,
    display_name,
    workflow_name,
    required_inputs,
    optional_inputs,
    input_is_list,
    return_types,
    return_names,
    output_is_list,
    description=None,
    is_output_node=False,
    validate_inputs=None,
    is_changed=None,
    debug=False,
):
    all_inputs = {"required": required_inputs, "optional": optional_inputs}

    # Initial class dictionary setup
    class_dict = {
        "INPUT_TYPES": classmethod(lambda cls: all_inputs),
        "CATEGORY": category,
        "RETURN_TYPES": return_types,
        "FUNCTION": cname,
        "INPUT_IS_LIST": input_is_list,
        "OUTPUT_IS_LIST": output_is_list,
        "OUTPUT_NODE": is_output_node,
        "RETURN_NAMES": return_names,
        "VALIDATE_INPUTS": validate_inputs,
        "IS_CHANGED": is_changed,
        "DESCRIPTION": description,
        cname: process_function,
    }
    class_dict = {k: v for k, v in class_dict.items() if v is not None}

    if debug:
        logger = logging.getLogger(cname)
        for key, value in class_dict.items():
            logger.info(f"{key}: {value}")

    assert (
        workflow_name not in NODE_CLASS_MAPPINGS
    ), f"Node class '{workflow_name} ({cname})' already exists!"
    assert (
        display_name not in NODE_DISPLAY_NAME_MAPPINGS.values()
    ), f"Display name '{display_name}' already exists!"
    assert (
        node_class not in NODE_CLASS_MAPPINGS.values()
    ), f"Only one method from '{node_class} can be used as a ComfyUI node.'"

    if node_class:
        for key, value in class_dict.items():
            setattr(node_class, key, value)
    else:
        node_class = type(workflow_name, (object,), class_dict)

    NODE_CLASS_MAPPINGS[workflow_name] = node_class
    NODE_DISPLAY_NAME_MAPPINGS[workflow_name] = display_name


def _is_static_method(cls, attr):
    """Check if a method is a static method."""
    if cls is None:
        return False
    attr_value = inspect.getattr_static(cls, attr, None)
    is_static = isinstance(attr_value, staticmethod)
    return is_static


def _is_class_method(cls, attr):
    if cls is None:
        return False
    attr_value = inspect.getattr_static(cls, attr, None)
    is_class_method = isinstance(attr_value, classmethod)
    return is_class_method


def _get_node_class(func):
    split_name = func.__qualname__.split(".")

    if len(split_name) > 1:
        class_name = split_name[-2]
        node_class = globals().get(class_name, None)
        if node_class is None and hasattr(func, "__globals__"):
            node_class = func.__globals__.get(class_name, None)
        return node_class
    return None
