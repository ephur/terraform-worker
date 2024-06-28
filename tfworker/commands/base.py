from typing import TYPE_CHECKING, Any, Dict

import click
from pydantic import ValidationError

import tfworker.commands.config as c
import tfworker.util.log as log
from tfworker.authenticators.collection import AuthenticatorsCollection
from tfworker.definitions import DefinitionsCollection

# from tfworker.plugins import PluginsCollection
from tfworker.providers.collection import ProvidersCollection
from tfworker.types.app_state import AppState
from tfworker.util.cli import handle_config_error

if TYPE_CHECKING:
    from tfworker.backends.base import BaseBackend
    from tfworker.types import CLIOptionsRoot


class BaseCommand:
    """
    BaseCommand is the base class for all commands in the worker utility,
    it primarily handles configuration file loading and validation

    Loading Configuration includes:
    - Loading the configuration file
        - proper options objects
        - dictionary representing the config file
    - Loading the providers (100% config file)
    - Loading the backend
    - Loading the handlers
    - Loading the plugins/providers
    - Loading global substitution vars
    - Loading / handling
        - template_vars
        - remote_vars
        - terraform_vars

    Move definitions to the terraform command, it has options/configurations required
    for definitions.

    GOAL: Do not require the TerraformCommand or other sub-commands to override
    the __init__ method!
    """

    def __init__(self, deployment: str | None = None):
        """
        initialize the base command with the deployment
        """
        app_state: AppState = click.get_current_context().obj
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

        # load the handlers
        # configure a backend to put on the app_state
        return

    @staticmethod
    def _init_authenticators(
        root_options: "CLIOptionsRoot",
    ) -> AuthenticatorsCollection:
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
        providers_config: ProvidersCollection, authenticators: AuthenticatorsCollection
    ) -> ProvidersCollection:
        """
        Initialize the providers collection based on the provided configuration, it will
        add information for providers that require authentication configurations

        Args:
            providers_config (ProvidersCollection): The providers configuration
            authenticators (AuthenticatorsCollection): The authenticators collection

        Returns:
            ProvidersCollection: The initialized providers collection
        """
        try:
            providers = ProvidersCollection(providers_config, authenticators)
        except ValidationError as e:
            handle_config_error(e)
        log.debug(
            f"initialized providers {[x for x in providers.keys()]}",
        )
        return providers

    @staticmethod
    def _init_definitions(definitions_config: Dict[str, Any]) -> DefinitionsCollection:
        """
        Initialize the definitions collection based on the provided configuration,

        Args:
            definitions_config (Dict[str, Any]): The definitions configuration
        """
        # look for any limit options on the app_state
        definitions = DefinitionsCollection(
            definitions_config, limiter=c.find_limiter()
        )
        log.debug(
            f"initialized definitions {[x for x in definitions.keys()]}",
        )
        return definitions

    @staticmethod
    def _init_backend_(app_state: AppState) -> "BaseBackend":
        """
        returns the initialized backend
        """
        from tfworker.backends import select_backend

        try:
            be = select_backend(
                app_state.loaded_config.worker_options["backend"],
                app_state.deployment,
                app_state.authenticators,
                app_state.definitions,
            )
        except BackendError as e:
            log.error(e)
            log.error(e.help)
            click.get_current_context().exit(1)
        log.debug(f"initialized backend {be.tag}")

        # if backend_plans is requested, check if backend supports it
        backend_plans = app_state.root_options.backend_plans
        if backend_plans:
            log.trace(f"backend_plans requested, checking if {be.tag} supports it")
            if not be.plan_storage:
                log.error(f"backend {be.tag} does not support backend_plans")
                click.get_current_context().exit(1)
        return be

        # # if backend_plans is requested, check if backend supports it
        # self._backend_plans = self._resolve_arg("backend_plans")
        # if self._backend_plans:
        #     if not self._backend.plan_storage:
        #         click.secho(
        #             f"backend {self._backend.tag} does not support backend_plans",
        #             fg="red",
        #         )
        #         raise SystemExit(1)

        # # initialize handlers collection
        # click.secho("Initializing handlers", fg="green")
        # try:
        #     self._handlers = HandlersCollection(rootc.handlers_odict)
        # except (UnknownHandler, HandlerError, TypeError) as e:
        #     click.secho(e, fg="red")
        #     raise SystemExit(1)

        # # allow a backend to implement handlers as well since they already control the provider session
        # if self._backend.handlers and self._backend_plans:
        #     self._handlers.update(self._backend.handlers)

        # # list enabled handlers
        # click.secho("Enabled handlers:", fg="green")
        # for h in self._handlers:
        #     click.secho(f"  {h}", fg="green")

    # @property
    # def authenticators(self):
    #     return self._authenticators

    # @property
    # def backend(self):
    #     return self._backend

    # @property
    # def providers(self):
    #     return self._providers

    # @property
    # def definitions(self):
    #     return self._definitions

    # @property
    # def plugins(self):
    #     return self._plugins

    # @property
    # def temp_dir(self):
    #     return self._temp_dir

    # @property
    # def repository_path(self):
    #     return self._repository_path

    # def _execute_handlers(self, action, stage, **kwargs):
    #     """Execute all ready handlers for supported actions"""
    #     for h in self._handlers:
    #         if action in h.actions and h.is_ready():
    #             h.execute(action, stage, **kwargs)

    # def _resolve_arg(self, name):
    #     """Resolve argument in order of precedence:
    #     1) CLI argument
    #     2) Config file
    #     """
    #     if name in self._args_dict and self._args_dict[name] is not None:
    #         return self._args_dict[name]
    #     if name in self._rootc.worker_options_odict:
    #         return self._rootc.worker_options_odict[name]
    #     return None
