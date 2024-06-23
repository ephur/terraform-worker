import inspect
import io
import json
import os
import pathlib
from typing import Any, Dict, List, Type, Union

import click
import hcl2
import jinja2
import yaml
from jinja2.runtime import StrictUndefined
from pydantic import BaseModel

# from tfworker.types.app_state import AppState
import tfworker.types.cli_options
import tfworker.util.log as log


def load_config(config_file: str, config_vars: Dict[str, str]) -> Dict[str, Any]:
    """
    Load the configuration file.

    Args:
        config_file (str): The path to the configuration file.
        config_vars (Dict[str, str]): A dictionary of configuration variables.

    Returns:
        Dict[str, Any]: The loaded configuration.
    """
    log.trace(f"loading config file: {config_file}")
    rendered_config = _process_template(config_file, _get_full_config_vars(config_vars))
    log.safe_trace(f"rendered config: {rendered_config}")
    if config_file.endswith(".hcl"):
        return hcl2.loads(rendered_config)["terraform"]
    return yaml.safe_load(rendered_config)["terraform"]


# def resolve_model_with_cli_options(ctx: click.Context, model_classes: list[type[BaseModel]]):
#     """
#     Resolve the model with the CLI options.

#     Args:
#         ctx (click.Context): The click context.
#         model_classes (list[type[BaseModel]]): The model classes to resolve.
#     """
#     if not hasattr(ctx.obj, "loaded_config") or ctx.obj.loaded_config is None:
#         raise ValueError("loaded_config is not set on the AppState object")

#     # for each field check if it's any of the model classes
#     skip_param_sources = [click.core.ParameterSource.ENVIRONMENT, click.core.ParameterSource.COMMANDLINE]
#     log.trace(f"not overwriting param sources: {skip_param_sources}")
#     for field in ctx.obj.model_fields_set:
#         log.trace(f"checking field: {field}")
#         model = getattr(ctx.obj, field)
#         if not any(isinstance(model, model_class) for model_class in model_classes):
#             continue

#         for model_class in model_classes:
#             if isinstance(model, model_class):
#                 log.trace(f"model {field}: matches model_class {model_class.__name__}")
#                 for k, v in ctx.obj.loaded_config["worker_options"].items():
#                     if k in model.model_fields:
#                         print(f"CTX Param Source {ctx.get_parameter_source(k)}")
#                         if ctx.get_parameter_source(k) in skip_param_sources:
#                             continue
#                         print(f"Setting {k} to {v} on {field}")
#                         setattr(model, k, v)

def get_cli_options_model_classes() -> List[Type[BaseModel]]:
    """
    Get all model classes from tfworker.types.cli_options that inherit from BaseModel
    and have names prefixed with 'CLIOptions'.

    Returns:
        List[Type[BaseModel]]: List of model classes.
    """
    cli_options_module = tfworker.types.cli_options
    model_classes = []

    for name, obj in inspect.getmembers(cli_options_module, inspect.isclass):
        if name.startswith("CLIOptions") and issubclass(obj, BaseModel):
            model_classes.append(obj)

    return model_classes

def resolve_model_with_cli_options(ctx: click.Context, model_classes: List[Type[BaseModel]]):
    """
    Resolve the model with the CLI options.

    Args:
        ctx (click.Context): The click context.
        model_classes (List[Type[BaseModel]]): The model classes to resolve.
    """
    if not hasattr(ctx.obj, "loaded_config") or ctx.obj.loaded_config is None:
        raise ValueError("loaded_config is not set on the AppState object")

    skip_param_sources = [click.core.ParameterSource.ENVIRONMENT, click.core.ParameterSource.COMMANDLINE]
    log.trace(f"not overwriting param sources: {skip_param_sources}")

    for field in ctx.obj.model_fields_set:
        model = getattr(ctx.obj, field)
        _update_model_if_match(ctx, model_classes, field, model, skip_param_sources)


def _update_model_if_match(ctx: click.Context, model_classes: List[Type[BaseModel]], field: str, model: BaseModel, skip_param_sources: List[click.core.ParameterSource]):
    """
    Update the model if it matches any of the model classes.

    Args:
        ctx (click.Context): The click context.
        model_classes (List[Type[BaseModel]]): The model classes to check against.
        field (str): The field name.
        model (BaseModel): The model instance.
        skip_param_sources (List[click.core.ParameterSource]): List of parameter sources to skip.
    """
    log.trace(f"checking field: {field}")
    if any(isinstance(model, model_class) for model_class in model_classes):
        for model_class in model_classes:
            if isinstance(model, model_class):
                log.trace(f"model {field}: matches model_class {model_class.__name__}")
                _set_model_parameters(ctx, model, field, skip_param_sources)


def _set_model_parameters(ctx: click.Context, model: BaseModel, field: str, skip_param_sources: List[click.core.ParameterSource]):
    """
    Set the parameters on the model.

    Args:
        ctx (click.Context): The click context.
        model (BaseModel): The model instance.
        field (str): The field name.
        skip_param_sources (List[click.core.ParameterSource]): List of parameter sources to skip.
    """
    for k, v in ctx.obj.loaded_config["worker_options"].items():
        if k in model.model_fields:
            log.trace(f"CTX Param Source {ctx.get_parameter_source(k)}")
            if ctx.get_parameter_source(k) in skip_param_sources:
                continue
            log.trace(f"Setting {k} to {v} on {field}")
            setattr(model, k, v)



def _process_template(config_file: str, config_vars: Dict[str, str]) -> str:
    """
    Process the Jinja2 template.
    """
    try:
        template_reader = io.StringIO()
        jinja_env = jinja2.Environment(
            undefined=StrictUndefined,
            loader=jinja2.FileSystemLoader(pathlib.Path(config_file).parents[0]),
        )
        template_config = jinja_env.get_template(pathlib.Path(config_file).name)
        template_config.stream(**config_vars).dump(template_reader)
    except jinja2.exceptions.UndefinedError as e:
        log.safe_error(
            f"Jinja2 Enironment\n{json.dumps(log.redact_items(config_vars), indent=2)}"
        )
        log.error(f"configuration file contains invalid template substitutions: {e}")
        raise SystemExit(1)

    return template_reader.getvalue()


def template_items(config_vars: Dict[str, str], return_as_dict=False, get_env=True):
    rvals = {}
    click.secho(f"Debbuging self.__dict__ {self.__dict__.items()}", fg="red")
    for k, v in self.__dict__.items():
        if k == "config_var":
            try:
                rvals["var"] = get_config_var_dict(v)
            except ValueError as e:
                click.secho(
                    f'Invalid config-var specified: "{e}" must be in format key=value',
                    fg="red",
                )
                raise SystemExit(1)
        else:
            rvals[k] = v
    if get_env is True:
        rvals["env"] = dict()
        for k, v in os.environ.items():
            rvals["env"][k] = v
    if return_as_dict:
        return rvals
    return rvals.items()


def _get_full_config_vars(config_vars: Dict[str, str]) -> Dict[str, Any]:
    """
    Get the full configuration variables.
    """
    original_config_vars = dict(config_vars)
    config_vars["var"] = dict()
    for k, v in original_config_vars.items():
        config_vars["var"][k] = v
    del original_config_vars

    # add os.environ to config_vars
    config_vars["env"] = dict()
    for k, v in os.environ.items():
        config_vars["env"][k] = v

    return config_vars


# def load_config(self):
#     """
#     Load the configuration file.
#     """
#     if not self.config_file:
#         return

#     self._config_file_exists()
#     rendered_config = self._process_template()

#     if self.config_file.endswith(".hcl"):
#         self.config = ordered_config_load_hcl(rendered_config)
#     else:
#         self.config = ordered_config_load(rendered_config)

#     # Decorate the RootCommand with the config values
#     self.tf = self.config.get("terraform", dict())
#     self._pullup_keys()
#     self._merge_args()

# def _config_file_exists(self):
#     """
#     Check if the configuration file exists.
#     """
#     if not os.path.exists(self.config_file):
#         click.secho(
#             f"configuration file does not exist: {self.config_file}", fg="red"
#         )
#         raise SystemExit(1)


# def add_args(self, args):
#     """
#     Add a dictionary of args.

#     Args:
#         args (dict): A dictionary of arguments to add.
#     """
#     for k, v in args.items():
#         self.add_arg(k, v)

# def add_arg(self, k, v):
#     """
#     Add an argument to the state args.

#     Args:
#         k (str): The key of the argument.
#         v (any): The value of the argument.
#     """
#     setattr(self.args, k, v)
#     return None

# def _pullup_keys(self):
#     """
#     A utility function to place keys from the loaded config file directly on the RootCommand instance.
#     """
#     for k in [
#         "definitions",
#         "providers",
#         "handlers",
#         "remote_vars",
#         "template_vars",
#         "terraform_vars",
#         "worker_options",
#     ]:
#         if self.tf:
#             setattr(self, f"{k}_odict", self.tf.get(k, dict()))
#         else:
#             setattr(self, f"{k}_odict", None)

# def _merge_args(self):
#     """
#     Merge the worker options from the config file with the command line arguments.
#     """
#     for k, v in self.worker_options_odict.items():
#         self.add_arg(k, v)

# class StateArgs:
#     """
#     A class to hold arguments in the state for easier access.
#     """

#     def __iter__(self):
#         return iter(self.__dict__)

#     def __getitem__(self, name):
#         return self.__dict__[name]

#     def __repr__(self):
#         return str(self.__dict__)

#     def keys(self):
#         return self.__dict__.keys()

#     def items(self):
#         return self.__dict__.items()

#     def values(self):
#         return self.__dict__.values()


# def get_config_var_dict(config_vars):
#     """
#     Returns a dictionary of of key=value for each item provided as a command line substitution.

#     Args:
#         config_vars (list): A list of command line substitutions.

#     Returns:
#         dict: A dictionary of key=value pairs.
#     """
#     return_vars = dict()
#     for cv in config_vars:
#         try:
#             k, v = tuple(cv.split("="))
#             return_vars[k] = v
#         except ValueError:
#             raise ValueError(cv)
#     return return_vars


# def ordered_config_load_hcl(config: str) -> dict:
#     """
#     Load an hcl config, and replace templated items.
#     """
#     return hcl2.loads(config)


# def ordered_config_load(config: str) -> dict:
#     """
#     since python 3.7 the yaml loader is deterministic, so we can
#     use the standard yaml loader
#     """
#     try:
#         return yaml.load(config, Loader=yaml.FullLoader)
#     except yaml.YAMLError as e:
#         click.secho(f"error loading yaml/json: {e}", fg="red")
#         click.secho("the configuration that caused the error was\n:", fg="red")
#         for i, line in enumerate(config.split("\n")):
#             click.secho(f"{i + 1}: {line}", fg="red")
#         raise SystemExit(1)


# def rm_tree(base_path: Union[str, Path], inner: bool = False) -> None:
#     """
#     Recursively removes all files and directories.

#     Args:
#         base_path (Union[str, Path]): The base path to start removing files and directories from.
#         inner (bool, optional): Controls recrusion, if True only the inner files and directories are removed. Defaults to False.
#     """
#     parent: Path = Path(base_path)

#     for child in parent.glob("*"):
#         if child.is_file() or child.is_symlink():
#             child.unlink()
#         else:
#             rm_tree(child, inner=True)
#     if inner:
#         parent.rmdir()
