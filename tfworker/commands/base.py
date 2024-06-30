from typing import TYPE_CHECKING, Any, Dict

import click
from pydantic import BaseModel, ValidationError

import tfworker.commands.config as c
import tfworker.util.log as log
from tfworker.exceptions import BackendError, HandlerError
from tfworker.util.cli import handle_config_error

if TYPE_CHECKING:
    from tfworker.authenticators.collection import AuthenticatorsCollection  # pragma: no cover
    from tfworker.definitions import DefinitionsCollection  # pragma: no cover
    from tfworker.handlers.collection import HandlersCollection  # pragma: no cover
    from tfworker.backends.base import BaseBackend  # pragma: no cover
    from tfworker.providers.collection import ProvidersCollection  # pragma: no cover
    from .cli_options import CLIOptionsRoot  # pragma: no cover
    from tfworker.app_state import AppState  # pragma: no cover


class BaseCommand:
    """
    Base command class that initializes the application state
    """

    def __init__(self, deployment: str | None = None) -> None:
        """
        initialize the base command with the deployment

        Args:
            deployment (str | None): The deployment name

        """
        app_state: "AppState" = click.get_current_context().obj
        app_state.deployment = deployment
        c.resolve_model_with_cli_options(app_state)
        # if logging level is changed via config, it won't affect stuff before this, but
        # can at least adjust it now
        log.log_level = log.LogLevel[app_state.root_options.log_level]
        app_state.authenticators = self._init_authenticators(app_state.root_options)
        app_state.providers = self._init_providers(
            app_state.loaded_config.providers, app_state.authenticators
        )
        app_state.definitions = self._init_definitions(
            app_state.loaded_config.definitions
        )
        app_state.backend = self._init_backend_(app_state)
        app_state.handlers = self._init_handlers(app_state.loaded_config.handlers)

    @staticmethod
    def _init_authenticators(
        root_options: "CLIOptionsRoot",
    ) -> "AuthenticatorsCollection":
        from tfworker.authenticators.collection import AuthenticatorsCollection
        """
        Initialize the authenticators collection for the application state

        Args:
            root_options (CLIOptionsRoot): The root options object

        Returns:
            AuthenticatorsCollection: The initialized authenticators collection
        """
        authenticators = AuthenticatorsCollection(root_options)
        log.debug(
            f"initialized authentiactors {[x.tag for x in authenticators.keys()]}",
        )
        return authenticators

    @staticmethod
    def _init_providers(
        providers_config: "ProvidersCollection", authenticators: "AuthenticatorsCollection"
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

    @staticmethod
    def _init_definitions(definitions_config: Dict[str, Any]) -> "DefinitionsCollection":
        """
        Initialize the definitions collection based on the provided configuration,

        Args:
            definitions_config (Dict[str, Any]): The definitions configuration
        """
        # look for any limit options on the app_state
        from tfworker.definitions import DefinitionsCollection

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

    @staticmethod
    def _init_backend_(app_state: "AppState") -> "BaseBackend":
        """
        Returns the initialized backend.

        Args:
            app_state (AppState): The current application state.

        Returns:
            BaseBackend: The initialized backend.

        """
        backend_config = app_state.loaded_config.worker_options["backend"]

        be = BaseCommand._select_backend(
            backend_config,
            app_state.deployment,
            app_state.authenticators,
            app_state.definitions,
        )

        BaseCommand._check_backend_plans(app_state.root_options.backend_plans, be)

        log.debug(f"initialized backend {be.tag}")
        return be

    @staticmethod
    def _select_backend(
        backend_config, deployment, authenticators, definitions
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
        from tfworker.backends import select_backend

        try:
            return select_backend(
                backend_config,
                deployment,
                authenticators,
                definitions,
            )
        except BackendError as e:
            log.error(e)
            log.error(e.help)
            click.get_current_context().exit(1)

    @staticmethod
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

    @staticmethod
    def _init_handlers(handlers_config: Dict[str, Any]) -> "HandlersCollection":
        """
        Initialize the handlers collection based on the provided configuration.

        Args:
            handlers_config (Dict[str, Any]): Configuration for the handlers.

        Returns:
            HandlersCollection: The initialized handlers collection.

        """
        from tfworker.handlers.collection import HandlersCollection
        parsed_handlers = BaseCommand._parse_handlers(handlers_config)
        BaseCommand._add_universal_handlers(parsed_handlers)

        log.trace(f"parsed handlers {parsed_handlers}")

        handlers = HandlersCollection(parsed_handlers)
        log.debug(f"initialized handlers {[x for x in handlers.keys()]}")

        BaseCommand._check_handlers_ready(handlers)
        return handlers

    @staticmethod
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
            config = BaseCommand._validate_handler_config(k, v)
            parsed_handlers[k] = BaseCommand._initialize_handler(k, config)
        return parsed_handlers

    @staticmethod
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
            return hr.get_handler_config_model(handler_name).model_validate(
                handler_config
            )
        except ValidationError as e:
            handle_config_error(e)

    @staticmethod
    def _initialize_handler(handler_name: str, config: BaseModel) -> Any:
        """
        Initializes a handler with the given configuration.

        Args:
            handler_name (str): The name of the handler.
            config (BaseModel): The validated configuration model.

        Returns:
            Any: The initialized handler.

        Raises:
            HandlerError: If there is an error initializing the handler.
        """
        from tfworker.handlers.registry import HandlerRegistry as hr
        try:
            return hr.get_handler(handler_name)(config)
        except HandlerError as e:
            log.error(e)
            click.get_current_context().exit(1)

    @staticmethod
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
                parsed_handlers[h] = hr.get_handler(h)

    @staticmethod
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
