import typing as t

import click
from pydantic import BaseModel
from pydantic.fields import PydanticUndefined


def pydantic_to_click(pydantic_model: t.Type[BaseModel]) -> click.Command:
    """Convert a Pydantic model to a Click command.

    Args:
        pydantic_model: Pydantic model to convert.

    Returns:
        Click command.
    """

    def decorator(func):
        model_types = t.get_type_hints(pydantic_model)
        for fname, fdata in pydantic_model.model_fields.items():
            description = fdata.description or ""
            default = fdata.default
            multiple = False

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
            else:
                raise ValueError(f"Unsupported type {model_types[fname]}")

            c_option_args = [f"--{fname.replace('_', '-')}"]
            c_option_kwargs = {
                "help": description,
                "default": default,
                "type": option_type,
                "required": fdata.is_required(),
                "multiple": multiple,
            }

            if option_type == click.BOOL:
                c_option_args = [
                    f"--{fname.replace('_', '-')}/--no-{fname.replace('_', '-')}"
                ]
                del c_option_kwargs["type"]

            func = click.option(*c_option_args, **c_option_kwargs)(func)
        return func

    return decorator
