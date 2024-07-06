import sys
from unittest.mock import MagicMock, patch

import click
import pytest
from pydantic import ValidationError
from pydantic_core import InitErrorDetails

import tfworker.util.log as log
from tfworker.app_state import AppState
from tfworker.authenticators import AuthenticatorsCollection, BaseAuthenticator
from tfworker.backends.base import BaseBackend
from tfworker.cli_options import CLIOptionsRoot
from tfworker.commands.base import BaseCommand, _init_authenticators
from tfworker.definitions.collection import DefinitionsCollection
from tfworker.exceptions import TFWorkerException
from tfworker.handlers.collection import HandlersCollection
from tfworker.providers.collection import ProvidersCollection
from tfworker.types import ConfigFile
from tfworker.util.log import LogLevel


@pytest.fixture
def mock_cli_options_root():
    mock_root = MagicMock(spec=CLIOptionsRoot)
    mock_root.log_level = "DEBUG"
    mock_root.backend = {"some_backend_config": "value"}
    mock_root.backend_plans = False
    mock_root.backend_prefix = "prefix_{deployment}"
    return mock_root


@pytest.fixture
def mock_loaded_config():
    mock_config = MagicMock(spec=ConfigFile)
    return mock_config


@pytest.fixture
def mock_app_state(mock_cli_options_root, mock_loaded_config):
    mock_state = MagicMock(spec=AppState)
    mock_state.authenticators = MagicMock(spec=AuthenticatorsCollection)
    mock_state.root_options = mock_cli_options_root
    mock_state.loaded_config = mock_loaded_config
    mock_state.loaded_config.providers = {}
    mock_state.loaded_config.definitions = {}
    mock_state.loaded_config.handlers = {}
    return mock_state


@pytest.fixture
def mock_click_context(mock_app_state):
    ctx = MagicMock(spec=click.Context)
    ctx.obj = mock_app_state
    return ctx


def mock_validation_error():
    errors = [
        InitErrorDetails(
            **{
                "loc": ("mock_field", "mock_field"),
                "input": "mock_input",
                "ctx": {"error": "error message"},
                "type": "value_error",
            }
        )
    ]
    return ValidationError.from_exception_data("invalid config", errors)


class TestBaseCommand:
    @pytest.fixture(autouse=True)
    def setup_method(
        self, mocker, mock_cli_options_root, mock_app_state, mock_click_context
    ):
        self.mock_resolve_model_with_cli_options = mocker.patch(
            "tfworker.commands.config.resolve_model_with_cli_options"
        )
        self.mock_log_level = mocker.patch(
            "tfworker.util.log.LogLevel", {"DEBUG": LogLevel.DEBUG}
        )
        self.mock_init_authenticators = mocker.patch(
            "tfworker.commands.base._init_authenticators",
            return_value=MagicMock(spec=AuthenticatorsCollection),
        )
        self.mock_init_providers = mocker.patch(
            "tfworker.commands.base._init_providers",
            return_value=MagicMock(spec=ProvidersCollection),
        )
        self.mock_init_backend_ = mocker.patch(
            "tfworker.commands.base._init_backend_",
            return_value=MagicMock(spec=BaseBackend),
        )
        self.mock_init_definitions = mocker.patch(
            "tfworker.commands.base._init_definitions",
            return_value=MagicMock(spec=DefinitionsCollection),
        )
        self.mock_init_handlers = mocker.patch(
            "tfworker.commands.base._init_handlers",
            return_value=MagicMock(spec=HandlersCollection),
        )
        self.mock_click_get_current_context = mocker.patch(
            "click.get_current_context", return_value=mock_click_context
        )

    def test_base_command_initialization_with_ctx(self, mock_click_context):
        base_command = BaseCommand(ctx=mock_click_context)
        assert base_command.ctx is mock_click_context
        assert base_command.app_state == mock_click_context.obj
        self.mock_resolve_model_with_cli_options.assert_called_once_with(
            mock_click_context.obj
        )
        self.mock_init_authenticators.assert_called_once_with(
            mock_click_context.obj.root_options
        )
        self.mock_init_providers.assert_called_once()
        self.mock_init_backend_.assert_called_once()
        self.mock_init_definitions.assert_called_once()
        self.mock_init_handlers.assert_called_once()
        assert base_command.app_state.root_options.backend_prefix == "prefix_None"

    def test_base_command_initialization_with_app_state(self, mock_app_state):
        base_command = BaseCommand(app_state=mock_app_state)
        assert base_command.ctx.obj is mock_app_state
        assert base_command.app_state == mock_app_state
        self.mock_resolve_model_with_cli_options.assert_called_once_with(mock_app_state)
        self.mock_init_authenticators.assert_called_once_with(
            mock_app_state.root_options
        )
        self.mock_init_providers.assert_called_once()
        self.mock_init_backend_.assert_called_once()
        self.mock_init_definitions.assert_called_once()
        self.mock_init_handlers.assert_called_once()
        assert base_command.app_state.root_options.backend_prefix == "prefix_None"

    def test_base_command_initialization_with_deployment(self, mock_click_context):
        deployment_name = "test_deployment"
        base_command = BaseCommand(deployment=deployment_name, ctx=mock_click_context)
        assert base_command.ctx is mock_click_context
        assert base_command.app_state.deployment == deployment_name
        assert (
            base_command.app_state.root_options.backend_prefix
            == f"prefix_{deployment_name}"
        )


class TestInitAuthenticators:
    def test_init_authenticators_success(self, mock_cli_options_root):
        mock_authenticators_collection = MagicMock(spec=AuthenticatorsCollection)
        mock_authenticators_collection._authenticators = {
            "mock_authenticator": MagicMock(spec=BaseAuthenticator),
        }
        with patch(
            "tfworker.authenticators.collection.AuthenticatorsCollection",
            return_value=mock_authenticators_collection,
        ):
            with patch.object(log, "debug") as mock_log_debug:
                authenticators = _init_authenticators(mock_cli_options_root)
                assert authenticators == mock_authenticators_collection
                patch(
                    "tfworker.authenticators.collection.AuthenticatorsCollection",
                    return_value=mock_authenticators_collection,
                )
                # Ensure that the call to log.debug is correct
                mock_log_debug.assert_called_once_with(
                    f"initialized authenticators {[mock_authenticators_collection[key].tag for key in mock_authenticators_collection.keys()]}",
                )

    # def test_init_authenticators_tfworker_exception(self, mock_cli_options_root):
    #     with patch('tfworker.commands.base.AuthenticatorsCollection', side_effect=TFWorkerException("authenticator error")):
    #         with patch('click.get_current_context') as mock_get_current_context:
    #             mock_ctx = MagicMock(spec=click.Context)
    #             mock_get_current_context.return_value = mock_ctx
    #             with patch.object(log, 'error') as mock_log_error:
    #                 with pytest.raises(SystemExit):
    #                     _init_authenticators(mock_cli_options_root)
    #                 mock_log_error.assert_called_once_with("authenticator error")
    #                 mock_ctx.exit.assert_called_once_with(1)

    def test_init_authenticators_tfworker_exception(self, mock_cli_options_root):
        tfworker_exception = TFWorkerException("authenticator error")
        with patch(
            "tfworker.authenticators.collection.AuthenticatorsCollection",
            side_effect=tfworker_exception,
        ):
            with patch("click.get_current_context") as mock_get_current_context:
                mock_ctx = MagicMock(spec=click.Context)
                mock_get_current_context.return_value = mock_ctx
                mock_ctx.exit.side_effect = sys.exit
                with patch.object(log, "error") as mock_log_error:
                    with pytest.raises(SystemExit):
                        _init_authenticators(mock_cli_options_root)
                    mock_log_error.assert_called_once_with(tfworker_exception)
                    mock_ctx.exit.assert_called_once_with(1)
