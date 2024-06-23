from typing import TYPE_CHECKING, Any, Dict, Union

import click
from pydantic import BaseModel, ValidationError

import tfworker.commands.config as c
import tfworker.util.log as log
from tfworker.exceptions import BackendError, HandlerError, TFWorkerException
from tfworker.util.cli import handle_config_error

if TYPE_CHECKING:
    from tfworker.app_state import AppState  # pragma: no cover # noqa
    from tfworker.authenticators.collection import (  # pragma: no cover # noqa
        AuthenticatorsCollection,
    )
    from tfworker.backends import Backends, BaseBackend  # pragma: no cover
    from tfworker.definitions.collection import (  # pragma: no cover # noqa
        DefinitionsCollection,
    )
    from tfworker.handlers.collection import (  # pragma: no cover # noqa
        HandlersCollection,
    )
    from tfworker.providers.collection import (  # pragma: no cover # noqa
        ProvidersCollection,
    )

    from ..cli_options import CLIOptionsRoot  # pragma: no cover # noqa


class BaseCommand:
    """
    Base command class that initializes the application state
    """

    def __init__(
        self,
        deployment: str | None = None,
        ctx: click.Context | None = None,
        app_state: Union["AppState", None] = None,
    ) -> None:
        """
        initialize the base command with the deployment, exceptions are handled
        in all of the _init methods

        Args:
            deployment (str | None): The deployment name

        """
        self._ctx: click.Context
        self._app_state: "AppState"

        if ctx is not None:
            self._ctx = ctx
        else:
            self._ctx = click.get_current_context()

        if app_state is not None:
            self._app_state = app_state
        else:
            self._app_state = self._ctx.obj

        self._app_state.deployment = deployment
        c.resolve_model_with_cli_options(self._app_state)
        log.log_level = log.LogLevel[self._app_state.root_options.log_level]

        self._app_state.authenticators = _init_authenticators(
            self._app_state.root_options
        )
        self._app_state.providers = _init_providers(
            self._app_state.loaded_config.providers, self._app_state.authenticators
        )
        self._app_state.backend = _init_backend_(self._app_state)
        self._app_state.definitions = _init_definitions(
            self._app_state.loaded_config.definitions
        )
        self._app_state.handlers = _init_handlers(
            self._app_state.loaded_config.handlers
        )

        # with deployment name known, update the root options
        self._app_state.root_options.backend_prefix = (
            self._app_state.root_options.backend_prefix.format(deployment=deployment)
        )
        self._app_state.freeze()

    @property
    def ctx(self) -> click.Context:
        return self._ctx

    @property
    def app_state(self) -> "AppState":
        return self._app_state


def _init_authenticators(
    root_options: "CLIOptionsRoot",
) -> "AuthenticatorsCollection":
    """
    Initialize the authenticators collection for the application state

    Args:
        root_options (CLIOptionsRoot): The root options object

    Returns:
        AuthenticatorsCollection: The initialized authenticators collection
    """
    #    from tfworker.authenticators.collection import AuthenticatorsCollection
    import tfworker.authenticators.collection as c

    try:
        authenticators = c.AuthenticatorsCollection(root_options)
    except TFWorkerException as e:
        log.error(e)
        click.get_current_context().exit(1)

    log.debug(
        f"initialized authenticators {[x.tag for x in authenticators.keys()]}",
    )
    return authenticators


def _init_providers(
    providers_config: "ProvidersCollection",
    authenticators: "AuthenticatorsCollection",
) -> "ProvidersCollection":
    """
    Initialize the providers collection based on the provided configuration, it will
    add information for providers that require authentication configurations

    Args:
        providers_config (ProvidersCollection): The providers configuration
        authenticators (AuthenticatorsCollection): The authenticators collection

    Returns:
        ProvidersCollection: The initialized providers collection
    """
    from tfworker.providers.collection import ProvidersCollection

    try:
        providers = ProvidersCollection(providers_config, authenticators)
    except ValidationError as e:
        handle_config_error(e)

    log.debug(
        f"initialized providers {[x for x in providers.keys()]}",
    )
    return providers


def _init_definitions(definitions_config: Dict[str, Any]) -> "DefinitionsCollection":
    """
    Initialize the definitions collection based on the provided configuration,

    Args:
        definitions_config (Dict[str, Any]): The definitions configuration
    """
    # look for any limit options on the app_state
    from tfworker.definitions.collection import DefinitionsCollection

    try:
        definitions = DefinitionsCollection(
            definitions_config, limiter=c.find_limiter()
        )
        log.debug(
            f"initialized definitions {[x for x in definitions.keys()]}",
        )
    except ValueError as e:
        log.error(e)
        click.get_current_context().exit(1)

    return definitions


def _init_backend_(app_state: "AppState") -> "BaseBackend":
    """
    Returns the initialized backend.

    Args:
        app_state (AppState): The current application state.

    Returns:
        BaseBackend: The initialized backend.

    """
    backend_config = app_state.root_options.backend

    be = _select_backend(
        backend_config,
        app_state.deployment,
        app_state.authenticators,
    )

    _check_backend_plans(app_state.root_options.backend_plans, be)

    log.debug(f"initialized backend {be.tag}")
    return be


def _select_backend(
    backend: "Backends", deployment: str, authenticators: "AuthenticatorsCollection"
) -> "BaseBackend":
    """
    Selects and initializes the backend.

    Args:
        backend_config (dict): Configuration for the backend.
        deployment (str): The deployment name.
        authenticators (AuthenticatorsCollection): The authenticators collection.
        definitions (DefinitionsCollection): The definitions collection.

    Returns:
        BaseBackend: The initialized backend.

    Raises:
        BackendError: If there is an error selecting the backend.
    """
    try:
        return backend.value(authenticators, deployment=deployment)
    except BackendError as e:
        log.error(e)
        log.error(e.help)
        click.get_current_context().exit(1)


def _check_backend_plans(backend_plans, backend) -> None:
    """
    Checks if backend plans are supported by the backend.

    Args:
        backend_plans (bool): Flag indicating if backend plans are requested.
        backend (BaseBackend): The initialized backend.

    """
    if backend_plans:
        log.trace(f"backend_plans requested, checking if {backend.tag} supports it")
        if not backend.plan_storage:
            log.error(f"backend {backend.tag} does not support backend_plans")
            click.get_current_context().exit(1)


def _init_handlers(handlers_config: Dict[str, Any]) -> "HandlersCollection":
    """
    Initialize the handlers collection based on the provided configuration.

    Args:
        handlers_config (Dict[str, Any]): Configuration for the handlers.

    Returns:
        HandlersCollection: The initialized handlers collection.

    """
    from tfworker.handlers.collection import HandlersCollection

    parsed_handlers = _parse_handlers(handlers_config)
    _add_universal_handlers(parsed_handlers)

    log.trace(f"parsed handlers {parsed_handlers}")

    handlers = HandlersCollection(parsed_handlers)
    log.debug(f"initialized handlers {[x for x in handlers.keys()]}")

    _check_handlers_ready(handlers)
    return handlers


def _parse_handlers(handlers_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parses the handlers configuration into handler instances.

    Args:
        handlers_config (Dict[str, Any]): Configuration for the handlers.

    Returns:
        Dict[str, Any]: Parsed handler instances.

    """
    parsed_handlers = {}
    for k, v in handlers_config.items():
        log.trace(f"initializing handler {k}")
        log.trace(f"handler config: {v}")
        config = _validate_handler_config(k, v)
        parsed_handlers[k] = _initialize_handler(k, config)
    return parsed_handlers


def _validate_handler_config(
    handler_name: str, handler_config: Dict[str, Any]
) -> BaseModel:
    """
    Validates the configuration for a handler.

    Args:
        handler_name (str): The name of the handler.
        handler_config (Dict[str, Any]): The configuration for the handler.

    Returns:
        BaseModel: The validated configuration model.

    Raises:
        ValidationError: If the configuration is invalid.
    """
    from tfworker.handlers.registry import HandlerRegistry as hr

    try:
        return hr.get_handler_config_model(handler_name).model_validate(handler_config)
    except ValidationError as e:
        handle_config_error(e)


def _initialize_handler(handler_name: str, config: BaseModel) -> Any:
    """
    Initializes a handler with the given configuration.

    Args:
        handler_name (str): The name of the handler.
        config (BaseModel): The validated configuration model.

    Returns:
        Any: The initialized handler.
    """
    from tfworker.handlers.registry import HandlerRegistry as hr

    try:
        return hr.get_handler(handler_name)(config)
    except HandlerError as e:
        log.error(e)
        click.get_current_context().exit(1)


def _add_universal_handlers(parsed_handlers: Dict[str, Any]):
    """
    Adds universal handlers to the parsed handlers.

    Args:
        parsed_handlers (Dict[str, Any]): The parsed handlers.

    """
    from tfworker.handlers.registry import HandlerRegistry as hr

    for h in hr.list_universal_handlers():
        log.trace(f"initializing universal handler {h}")
        if h not in parsed_handlers.keys():
            parsed_handlers[h] = hr.get_handler(h)()


def _check_handlers_ready(handlers: "HandlersCollection"):
    """
    Checks if all handlers are ready.

    Args:
        handlers (HandlersCollection): The handlers collection.

    """
    for h, v in handlers.items():
        log.trace(f"checking if handler {h} is ready")
        if not v.is_ready:
            log.debug(f"handler {h} is not ready, removing it")
            handlers.pop(h)
