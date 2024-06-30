# Mock network sockets
import socket
from unittest.mock import MagicMock, patch

import click
import pytest
from pydantic import ValidationError

# Import the BaseCommand class
from tfworker.commands.base import BaseCommand
from tfworker.exceptions import BackendError

socket.socket = MagicMock()


class MockBackend:
    tag = "mock_backend"
    plan_storage = True


@pytest.fixture
def mock_app_state():
    from tfworker.types.app_state import AppState

    app_state = AppState()
    app_state.root_options = MagicMock()
    app_state.loaded_config = MagicMock()
    app_state.root_options.log_level = "DEBUG"
    app_state.loaded_config.providers = {}
    app_state.loaded_config.definitions = {}
    app_state.loaded_config.worker_options = {"backend": {}}
    app_state.loaded_config.handlers = {}
    return app_state


@pytest.fixture
def mock_cli_options():
    from tfworker.types import CLIOptionsRoot

    return CLIOptionsRoot(log_level="DEBUG")


@pytest.fixture
def mock_authenticators(mock_cli_options):
    from tfworker.authenticators.collection import AuthenticatorsCollection

    return AuthenticatorsCollection(mock_cli_options)


@pytest.fixture
def mock_providers():
    from tfworker.providers.collection import ProvidersCollection

    return ProvidersCollection({})


@pytest.fixture
def mock_definitions():
    from tfworker.definitions import DefinitionsCollection

    return DefinitionsCollection({})


@pytest.fixture
def mock_handlers():
    from tfworker.handlers.collection import HandlersCollection

    return HandlersCollection({})


@pytest.fixture
def mock_backend():
    class MockBackend:
        tag = "mock_backend"
        plan_storage = True

    return MockBackend()


@pytest.fixture
def cli_context(mock_app_state):
    # Setup the click context
    ctx = click.Context(click.Command("test_command"))
    ctx.obj = mock_app_state
    return ctx


def mock_select_backend(config, deployment, authenticators, definitions):
    return MockBackend()


def mock_get_handler_config_model(name):
    class MockBaseModel:
        @staticmethod
        def model_validate(data):
            return data

    return MockBaseModel


def mock_get_handler(name):
    class MockHandler:
        def __init__(self, config):
            self.config = config

        @staticmethod
        def is_ready():
            return True

    return MockHandler


def mock_list_universal_handlers():
    return ["universal_handler"]


def test_init(
    cli_context,
    mock_app_state,
    mock_cli_options,
    mock_authenticators,
    mock_providers,
    mock_definitions,
    mock_backend,
    mock_handlers,
):
    with patch("click.get_current_context", return_value=cli_context), patch(
        "tfworker.commands.config.resolve_model_with_cli_options"
    ), patch(
        "tfworker.commands.base.BaseCommand._init_authenticators",
        return_value=mock_authenticators,
    ), patch(
        "tfworker.commands.base.BaseCommand._init_providers",
        return_value=mock_providers,
    ), patch(
        "tfworker.commands.base.BaseCommand._init_definitions",
        return_value=mock_definitions,
    ), patch(
        "tfworker.commands.base.BaseCommand._init_backend_", return_value=mock_backend
    ), patch(
        "tfworker.commands.base.BaseCommand._init_handlers", return_value=mock_handlers
    ):

        base_command = BaseCommand(deployment="test_deployment")

        assert mock_app_state.deployment == "test_deployment"
        assert mock_app_state.authenticators == mock_authenticators
        assert mock_app_state.providers == mock_providers
        assert mock_app_state.definitions == mock_definitions
        assert mock_app_state.backend == mock_backend
        assert mock_app_state.handlers == mock_handlers


@patch("tfworker.authenticators.collection.AuthenticatorsCollection")
@patch("tfworker.util.log.debug")
def test_init_authenticators(
    mock_log_debug, mock_authenticators_collection, mock_cli_options
):
    # Create an instance of the mocked AuthenticatorsCollection
    mock_authenticators_instance = MagicMock()
    mock_authenticators_collection.return_value = mock_authenticators_instance

    # Call the method
    result = BaseCommand._init_authenticators(mock_cli_options)

    # Check that AuthenticatorsCollection was called with the correct arguments
    mock_authenticators_collection.assert_called_once_with(mock_cli_options)

    # Check that the method returns the correct instance
    assert result == mock_authenticators_instance

    # Check that the debug log was called with the correct message
    mock_log_debug.assert_called_once_with(
        f"initialized authentiactors {[x.tag for x in mock_authenticators_instance.keys()]}"
    )


@patch("tfworker.providers.collection.ProvidersCollection")
@patch("tfworker.util.cli.handle_config_error")
@patch("tfworker.util.log.debug")
def test_init_providers(
    mock_log_debug,
    mock_handle_config_error,
    mock_providers_collection,
    mock_providers,
    mock_authenticators,
):
    # Create an instance of the mocked ProvidersCollection
    mock_providers_instance = MagicMock()
    mock_providers_collection.return_value = mock_providers_instance

    # Call the method
    result = BaseCommand._init_providers(mock_providers, mock_authenticators)

    # Check that ProvidersCollection was called with the correct arguments
    mock_providers_collection.assert_called_once_with(
        mock_providers, mock_authenticators
    )

    # Check that the method returns the correct instance
    assert result == mock_providers_instance

    # Check that the debug log was called with the correct message
    mock_log_debug.assert_called_once_with(
        f"initialized providers {[x for x in mock_providers_instance.keys()]}"
    )


@patch("tfworker.providers.collection.ProvidersCollection")
@patch("tfworker.commands.base.handle_config_error")
def test_init_providers_validation_error(
    mock_handle_config_error,
    mock_providers_collection,
    cli_context,
    mock_providers,
    mock_authenticators,
):
    # Simulate a ValidationError
    mock_providers_collection.side_effect = ValidationError.from_exception_data(
        "provider", []
    )
    mock_handle_config_error.side_effect = click.exceptions.Exit

    with patch("click.get_current_context", return_value=cli_context):
        with pytest.raises(click.exceptions.Exit):
            BaseCommand._init_providers(mock_providers, mock_authenticators)
    # debug the test
    mock_handle_config_error.assert_called_once()

    # Ensure that ProvidersCollection was called with the correct arguments
    mock_providers_collection.assert_called_once_with(
        mock_providers, mock_authenticators
    )


@pytest.fixture
def mock_definitions_config():
    return {"definition_key": "definition_value"}


@patch("tfworker.definitions.DefinitionsCollection")
@patch("tfworker.commands.config.find_limiter")
@patch("tfworker.util.log.debug")
def test_init_definitions(
    mock_log_debug,
    mock_find_limiter,
    mock_definitions_collection,
    mock_definitions_config,
):
    # Mock the return value of find_limiter
    mock_limiter = MagicMock()
    mock_find_limiter.return_value = mock_limiter

    # Create an instance of the mocked DefinitionsCollection
    mock_definitions_instance = MagicMock()
    mock_definitions_collection.return_value = mock_definitions_instance

    # Call the method
    result = BaseCommand._init_definitions(mock_definitions_config)

    # Check that DefinitionsCollection was called with the correct arguments
    mock_definitions_collection.assert_called_once_with(
        mock_definitions_config, limiter=mock_limiter
    )

    # Check that the method returns the correct instance
    assert result == mock_definitions_instance

    # Check that the debug log was called with the correct message
    mock_log_debug.assert_called_once_with(
        f"initialized definitions {[x for x in mock_definitions_instance.keys()]}"
    )


@patch("tfworker.commands.base.BaseCommand._select_backend")
@patch("tfworker.commands.base.BaseCommand._check_backend_plans")
@patch("tfworker.util.log.debug")
def test_init_backend(
    mock_log_debug, mock_check_backend_plans, mock_select_backend, mock_app_state
):
    # Create an instance of the mocked backend
    mock_backend_instance = MagicMock()
    mock_backend_instance.tag = "mock_backend"
    mock_select_backend.return_value = mock_backend_instance

    # Call the method
    result = BaseCommand._init_backend_(mock_app_state)

    # Check that _select_backend was called with the correct arguments
    mock_select_backend.assert_called_once_with(
        mock_app_state.loaded_config.worker_options["backend"],
        mock_app_state.deployment,
        mock_app_state.authenticators,
        mock_app_state.definitions,
    )

    # Check that _check_backend_plans was called with the correct arguments
    mock_check_backend_plans.assert_called_once_with(
        mock_app_state.root_options.backend_plans, mock_backend_instance
    )

    # Check that the method returns the correct instance
    assert result == mock_backend_instance

    # Check that the debug log was called with the correct message
    mock_log_debug.assert_called_once_with(
        f"initialized backend {mock_backend_instance.tag}"
    )


@patch("tfworker.backends.select_backend")
@patch("tfworker.util.log.error")
def test_select_backend(mock_log_error, mock_select_backend):
    backend_config = {"key": "value"}
    deployment = "test_deployment"
    authenticators = MagicMock()
    definitions = MagicMock()

    # Create an instance of the mocked backend
    mock_backend_instance = MagicMock()
    mock_select_backend.return_value = mock_backend_instance

    # Call the method
    result = BaseCommand._select_backend(
        backend_config, deployment, authenticators, definitions
    )

    # Check that select_backend was called with the correct arguments
    mock_select_backend.assert_called_once_with(
        backend_config,
        deployment,
        authenticators,
        definitions,
    )

    # Check that the method returns the correct instance
    assert result == mock_backend_instance

    # Ensure no error was logged
    mock_log_error.assert_not_called()


@patch("tfworker.backends.select_backend")
@patch("tfworker.util.log.error")
def test_select_backend_error(mock_log_error, mock_select_backend):
    backend_config = {"key": "value"}
    deployment = "test_deployment"
    authenticators = MagicMock()
    definitions = MagicMock()

    # Simulate a BackendError
    mock_backend_error = BackendError("Backend error", help="Some help message")
    mock_select_backend.side_effect = mock_backend_error

    with patch("click.get_current_context") as mock_get_current_context:
        mock_get_current_context.return_value = MagicMock()

        # Call the method and expect a system exit
        with pytest.raises(click.exceptions.Exit):
            BaseCommand._select_backend(
                backend_config, deployment, authenticators, definitions
            )

        # Check that select_backend was called with the correct arguments
        mock_select_backend.assert_called_once_with(
            backend_config,
            deployment,
            authenticators,
            definitions,
        )

        # Check that the error was logged
        print(mock_log_error.call_args_list)
        mock_log_error.assert_any_call(mock_backend_error)
        mock_log_error.assert_any_call(mock_backend_error.help)

        # Check that the context's exit method was called
        mock_context.exit.assert_called_once_with(1)


if __name__ == "__main__":
    pytest.main()

# @patch('tfworker.util.cli.handle_config_error', MagicMock())
# @patch('tfworker.util.log.log', MagicMock())
# @patch('tfworker.backends.select_backend', mock_select_backend)
# @patch('tfworker.handlers.registry.HandlerRegistry.get_handler_config_model', mock_get_handler_config_model)
# @patch('tfworker.handlers.registry.HandlerRegistry.get_handler', mock_get_handler)
# @patch('tfworker.handlers.registry.HandlerRegistry.list_universal_handlers', mock_list_universal_handlers)
# def test_methods(cli_context, mock_app_state, mock_cli_options, mock_authenticators, mock_providers, mock_definitions, mock_handlers):
#     from tfworker.authenticators.collection import AuthenticatorsCollection
#     from tfworker.providers.collection import ProvidersCollection
#     from tfworker.definitions import DefinitionsCollection
#     from tfworker.handlers.collection import HandlersCollection

#     with patch('click.get_current_context', return_value=cli_context):
#         assert isinstance(BaseCommand._init_authenticators(mock_cli_options), AuthenticatorsCollection)
#         assert isinstance(BaseCommand._init_providers({}, mock_authenticators), ProvidersCollection)
#         assert isinstance(BaseCommand._init_definitions({}), DefinitionsCollection)
#         assert isinstance(BaseCommand._init_backend_(mock_app_state), MockBackend)
#         BaseCommand._check_backend_plans(True, MockBackend())

#         # Test handlers
#         assert isinstance(BaseCommand._init_handlers({}), HandlersCollection)
#         parsed_handlers = BaseCommand._parse_handlers({})
#         assert isinstance(parsed_handlers, dict)
#         config = BaseCommand._validate_handler_config("handler", {})
#         assert config == {}
#         handler = BaseCommand._initialize_handler("handler", config)
#         assert isinstance(handler, mock_get_handler("handler"))

#         # Check handlers ready
#         handlers = HandlersCollection({"handler": mock_get_handler("handler")({})})
#         BaseCommand._check_handlers_ready(handlers)

#         # Handlers not ready
#         class NotReadyHandler:
#             def __init__(self, config):
#                 self.config = config
#             @staticmethod
#             def is_ready():
#                 return False
#         not_ready_handlers = HandlersCollection({"handler": NotReadyHandler({})})
#         with pytest.raises(SystemExit):
#             BaseCommand._check_handlers_ready(not_ready_handlers)

#         # Check backend plans not supported
#         mock_backend = MockBackend()
#         mock_backend.plan_storage = False
#         with pytest.raises(SystemExit):
#             BaseCommand._check_backend_plans(True, mock_backend)

if __name__ == "__main__":
    pytest.main()
