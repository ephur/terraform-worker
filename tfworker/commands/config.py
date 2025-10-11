import inspect
import io
import json
import os
import pathlib
from typing import Any, Dict, List, Type, Union

import click
import jinja2
import yaml
from jinja2.runtime import StrictUndefined
from mergedeep import merge
from pydantic import BaseModel, ValidationError

import tfworker.util.log as log
from tfworker.app_state import AppState
from tfworker.custom_types.config_file import ConfigFile
from tfworker.util.cli import handle_config_error
from tfworker.util.hcl_parser import parse_string as parse_hcl_string

from .. import cli_options


def load_config(
    config_file: Union[str, List[str]], config_vars: Dict[str, str]
) -> ConfigFile:
    """Load one or more configuration files and merge them.

    Later files override values from earlier ones.
    """

    config_files = [config_file] if isinstance(config_file, str) else config_file
    merged_config: Dict[str, Any] = {}
    full_vars = _get_full_config_vars(config_vars)

    for cf in config_files:
        log.trace(f"loading config file: {cf}")
        rendered = _process_template(cf, full_vars)
        log.safe_trace(f"rendered config: {rendered}")

        if cf.endswith(".hcl"):
            loaded: Dict[str, Any] = parse_hcl_string(rendered)["terraform"]
        else:
            loaded = yaml.safe_load(rendered)["terraform"]

        merge(merged_config, loaded)

    try:
        parsed_config = ConfigFile.model_validate(merged_config)
    except ValidationError as e:
        handle_config_error(e)

    return parsed_config


def find_limiter() -> List[str] | None:
    """
    Find if any of the CLIOptions have a limit option.

    Returns:
        List[str] | None: The limiter if found, otherwise None
    """
    model_classes = get_cli_options_model_classes()
    available_classes = []

    for model_class in model_classes:
        log.trace(f"checking model class: {model_class.__name__} for limit")
        if model_class.model_fields.get("limit", None) is None:
            continue
        available_classes.append(model_class.__name__)

    app_state = click.get_current_context().obj
    for field in app_state.model_fields_set:
        model = getattr(app_state, field)
        if model.__class__.__name__ in available_classes:
            return model.limit


def log_limiter() -> None:
    """Log the limiter."""
    limiter = find_limiter()
    if limiter:
        log.info(f"limiting to {', '.join(limiter)}")


def get_cli_options_model_classes() -> List[Type[BaseModel]]:
    """
    Get all model classes from tfworker.custom_types.cli_options that inherit from BaseModel
    and have names prefixed with 'CLIOptions'.

    Returns:
        List[Type[BaseModel]]: List of model classes.
    """
    cli_options_module = cli_options
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
    for k, v in app_state.loaded_config.worker_options.items():
        if k in model.model_fields:
            if ctx.get_parameter_source(k) in skip_param_sources:
                log.trace(
                    f"skipping {k} as it is set via {ctx.get_parameter_source(k)}"
                )
                continue
            log.trace(f"Setting {k} to {v} on {field}")
            try:
                setattr(model, k, v)
            except ValidationError as e:
                handle_config_error(e)
    # Also need to add all the worker_options to the loaded_config
    for k in model.model_fields.keys():
        # if k not in app_state.loaded_config.worker_options:
        value = getattr(model, k)
        log.trace(
            f"setting {k}={value} to worker_options via {model.__class__.__name__}"
        )
        app_state.loaded_config.worker_options[k] = value


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
        click.get_current_context().exit(1)
    except jinja2.exceptions.TemplateNotFound as e:
        log.error(f"configuration file {config_file} not found: {e}")
        click.get_current_context().exit(1)
    except jinja2.exceptions.TemplateSyntaxError as e:
        log.error("configuration file contains invalid template syntax")
        log.error(f"File: {e.filename}; Line: {e.lineno}; Message: {e.message}")
        click.get_current_context().exit(1)

    return template_reader.getvalue()


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
