from typing import List, Optional
from unittest.mock import patch

import click
import pytest
from pydantic import BaseModel

from tfworker.util.cli import pydantic_to_click, validate_host


class ATestModel(BaseModel):
    str_field: str
    optional_str_field: Optional[str] = None
    int_field: int
    optional_int_field: Optional[int] = None
    float_field: float
    optional_float_field: Optional[float] = None
    bool_field: bool
    optional_bool_field: Optional[bool] = None
    list_str_field: List[str]
    optional_list_str_field: Optional[List[str]] = []


@click.command()
@pydantic_to_click(ATestModel)
def a_command():
    pass


def test_pydantic_to_click():
    command = a_command

    # Check that the command is a Click command
    assert isinstance(command, click.Command)

    # Check that the command has the correct options
    options = {option.name: option for option in command.params}
    assert set(options.keys()) == set(ATestModel.__annotations__.keys())

    # Check the types of the options
    assert isinstance(options["str_field"].type, click.types.StringParamType)
    assert isinstance(options["optional_str_field"].type, click.types.StringParamType)
    assert isinstance(options["int_field"].type, click.types.IntParamType)
    assert isinstance(options["optional_int_field"].type, click.types.IntParamType)
    assert isinstance(options["float_field"].type, click.types.FloatParamType)
    assert isinstance(options["optional_float_field"].type, click.types.FloatParamType)
    assert isinstance(options["bool_field"].type, click.types.BoolParamType)
    assert isinstance(options["optional_bool_field"].type, click.types.BoolParamType)
    assert isinstance(options["list_str_field"].type, click.types.StringParamType)
    assert isinstance(
        options["optional_list_str_field"].type, click.types.StringParamType
    )

    # Check the 'multiple' attribute of the options
    assert options["list_str_field"].multiple is True
    assert options["optional_list_str_field"].multiple is True

    # Check the 'required' attribute of the options
    assert options["str_field"].required is True
    assert options["optional_str_field"].required is False
    assert options["int_field"].required is True
    assert options["optional_int_field"].required is False
    assert options["float_field"].required is True
    assert options["optional_float_field"].required is False
    assert options["bool_field"].required is True
    assert options["optional_bool_field"].required is False
    assert options["list_str_field"].required is True
    assert options["optional_list_str_field"].required is False


class UnsupportedModel(BaseModel):
    unsupported_field: dict


def test_unsupported_type():
    with pytest.raises(ValueError, match=r"Unsupported type <class 'dict'>"):

        @pydantic_to_click(UnsupportedModel)
        def a_command():
            pass


def test_validate_host():
    """only linux and darwin are supported, and require 64 bit platforms"""
    with patch("tfworker.util.system.get_platform", return_value=("linux", "amd64")):
        assert validate_host() is None
    with patch("tfworker.util.system.get_platform", return_value=("darwin", "amd64")):
        assert validate_host() is None
    with patch("tfworker.util.system.get_platform", return_value=("darwin", "arm64")):
        assert validate_host() is None


def test_validate_host_invalid_machine():
    """ensure invalid machine types fail"""
    with patch("tfworker.util.cli.get_platform", return_value=("darwin", "i386")):
        with pytest.raises(NotImplementedError, match="running on i386"):
            validate_host()


def test_validate_host_invalid_os():
    """ensure invalid os types fail"""
    with patch("tfworker.util.cli.get_platform", return_value=("windows", "amd64")):
        with pytest.raises(NotImplementedError, match="running on windows"):
            validate_host()


def test_vlidate_host_invalid_os_machine():
    """ensure invalid os and machine types fail"""
    with patch("tfworker.util.cli.get_platform", return_value=("windows", "i386")):
        with pytest.raises(NotImplementedError) as e:
            validate_host()
        assert "running on windows" in str(e.value)
        assert "running on i386" in str(e.value)
