import typing as t
from enum import Enum

import click
from pydantic import BaseModel, ValidationError
from pydantic.fields import PydanticUndefined

import tfworker.util.log as log
from tfworker.util.system import get_platform


def handle_option_error(e: ValidationError) -> None:
    """Handle a Pydantic validation error.

    Rather than raising a click.BackOptionUsage, this function will capture and report
    all validation errors, instead of just the first one encountered.

    Args:
        e: Pydantic validation error.

    Raises:
        click.ClickBadOption: Pydantic validation error.
    """
    error_message = ["options error(s):"]
    try:
        for error in e.errors():
            # pydantic adds "Value error," to the beginning of the error message, so we remove it
            error_message.append(
                f"{error['loc'][0]}: {error['msg'].split(',', 1)[1].strip()}"
            )
    except IndexError:
        error_message.append(str(e))

    # use .format to work around python f-string limitation of not being able to use \n
    # log.msg(f"{'\\n  '.join(error_message)}", log.LogLevel.ERROR)
    log.error("{}".format("\n  ".join(error_message)))
    click.get_current_context().exit(1)


def handle_config_error(e: ValidationError) -> None:
    """Handle a Pydantic validation error.

    Args:
        e: Pydantic validation error.

    Raises:
        click.ClickBadOption: Pydantic validation error.
    """
    if e.error_count() == 1:
        error_message = ["config error:"]
    else:
        error_message = ["config errors:"]

    if hasattr(e, "ctx"):
        error_message.append(
            f"validation error while loading {e.ctx[0]} named {e.ctx[1]}"
        )
    for error in e.errors():
        error_message.append("  Details:")
        error_message.append(f"    Error Type: {error['type']}")
        error_message.append(f"    Error Loc: {error['loc']}")
        error_message.append(f"    Error Msg: {error['msg']}")
        error_message.append(f"    Input Value: {error['input']}")

    # use .format to work around python f-string limitation of not being able to use \n
    log.error("{}".format("\n  ".join(error_message)))
    click.get_current_context().exit(1)


def pydantic_to_click(pydantic_model: t.Type[BaseModel]) -> click.Command:
    """Convert a Pydantic model to a Click command.

    There are some limitations on types that are supported, custom validation
    needs done on the model for ENUM types in order to keep this generic enough
    to be usable and easily extendable

    Args:
        pydantic_model: Pydantic model to convert.

    Returns:
        Click command.
    """

    def decorator(func):
        model_types = t.get_type_hints(pydantic_model)
        for fname, fdata in reversed(sorted(pydantic_model.model_fields.items())):
            default = fdata.default
            multiple = False
            has_extra = fdata.json_schema_extra is not None

            c_option_kwargs = {
                "help": fdata.description,
                "required": fdata.is_required(),
            }

            if has_extra and fdata.json_schema_extra.get("env"):
                c_option_kwargs["envvar"] = fdata.json_schema_extra["env"]
                c_option_kwargs["show_envvar"] = True

            if model_types[fname] in [str, t.Optional[str]]:
                option_type = click.STRING
            elif model_types[fname] in [int, t.Optional[int]]:
                option_type = click.INT
            elif model_types[fname] in [float, t.Optional[float]]:
                option_type = click.FLOAT
            elif model_types[fname] in [bool, t.Optional[bool]]:
                option_type = click.BOOL
            elif model_types[fname] in [t.List[str], t.Optional[t.List[str]]]:
                option_type = click.STRING
                multiple = True
                if default is PydanticUndefined:
                    default = []
            elif isinstance(model_types[fname], type) and issubclass(
                model_types[fname], Enum
            ):
                option_type = click.STRING
            else:
                raise ValueError(f"Unsupported type {model_types[fname]}")
            c_option_kwargs["type"] = option_type
            c_option_kwargs["multiple"] = multiple
            c_option_kwargs["default"] = default

            c_option_args = [f"--{fname.replace('_', '-')}"]
            if has_extra and fdata.json_schema_extra.get("short_arg"):
                c_option_args.append(f"-{fdata.json_schema_extra['short_arg']}")

            if option_type == click.BOOL:
                c_option_args = [
                    f"--{fname.replace('_', '-')}/--no-{fname.replace('_', '-')}"
                ]
                del c_option_kwargs["type"]
            log.msg(
                f'generated option "{fname}" with params {c_option_args}, {c_option_kwargs} from {fdata}',
                log.LogLevel.TRACE,
            )
            func = click.option(*c_option_args, **c_option_kwargs)(func)
        return func

    return decorator


def validate_deployment(ctx, deployment, name):
    """Validate the deployment is no more than 32 characters."""
    if len(name) > 32:
        log.msg("deployment must be less than 32 characters", log.LogLevel.ERROR)
        raise SystemExit(1)
    if " " in name:
        log.msg("deployment must not contain spaces", log.LogLevel.ERROR)
        raise SystemExit(1)
    return name


def validate_host() -> None:
    """Ensure that the script is being run on a supported platform.

    Raises:
        NotImplemented: If the script is being run on an unsupported platform.
    """

    supported_opsys = ["darwin", "linux"]
    supported_machine = ["amd64", "arm64"]

    opsys, machine = get_platform()
    message = []

    if opsys not in supported_opsys:
        message.append(f"running on {opsys} is not supported")

    if machine not in supported_machine:
        message.append(f"running on {machine} is not supported")

    if message:
        raise NotImplementedError("\n".join(message))
