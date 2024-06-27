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
from pydantic import BaseModel, ValidationError

# from tfworker.types.app_state import AppState
import tfworker.types.cli_options
import tfworker.util.log as log
from tfworker.types.app_state import AppState
from tfworker.types.config_file import ConfigFile
from tfworker.util.cli import handle_config_error


def load_config(config_file: str, config_vars: Dict[str, str]) -> ConfigFile:
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
        loaded_config: Dict[Any, Any] = hcl2.loads(rendered_config)["terraform"]
    else:
        loaded_config: Dict[Any, Any] = yaml.safe_load(rendered_config)["terraform"]

    try:
        parsed_config = ConfigFile.model_validate(loaded_config)
    except ValidationError as e:
        handle_config_error(e)

    return parsed_config


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


def resolve_model_with_cli_options(
    app_state: AppState, model_classes: Union[List[Type[BaseModel]], None] = None
) -> None:
    """
    Resolve the model with the CLI options.

    Args:
        ctx (click.Context): The click context.
        model_classes (List[Type[BaseModel]]): The model classes to resolve.
    """
    if model_classes is None:
        model_classes = get_cli_options_model_classes()

    if not hasattr(app_state, "loaded_config") or app_state.loaded_config is None:
        raise ValueError("loaded_config is not set on the AppState object")

    skip_param_sources = [
        click.core.ParameterSource.ENVIRONMENT,
        click.core.ParameterSource.COMMANDLINE,
    ]
    log.trace(f"not overwriting param sources: {skip_param_sources}")

    for field in app_state.model_fields_set:
        model = getattr(app_state, field)
        _update_model_if_match(
            app_state, model_classes, field, model, skip_param_sources
        )


def _update_model_if_match(
    app_state: AppState,
    model_classes: List[Type[BaseModel]],
    field: str,
    model: BaseModel,
    skip_param_sources: List[click.core.ParameterSource],
):
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
                _set_model_parameters(app_state, model, field, skip_param_sources)


def _set_model_parameters(
    app_state: AppState,
    model: BaseModel,
    field: str,
    skip_param_sources: List[click.core.ParameterSource],
):
    """
    Set the parameters on the model.

    Args:
        ctx (click.Context): The click context.
        model (BaseModel): The model instance.
        field (str): The field name.
        skip_param_sources (List[click.core.ParameterSource]): List of parameter sources to skip.
    """
    ctx = click.get_current_context()

    log.trace(f"{app_state.loaded_config.worker_options}")
    for k, v in app_state.loaded_config.worker_options.items():
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
        log.safe_error(f"Jinja2 Enironment\n{json.dumps(config_vars, indent=2)}")
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
