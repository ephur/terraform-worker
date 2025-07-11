from pathlib import Path

from pydantic import ConfigDict, Field

from tfworker import cli_options
from tfworker.authenticators import AuthenticatorsCollection
from tfworker.backends import BaseBackend
from tfworker.custom_types import ConfigFile, FreezableBaseModel
from tfworker.definitions import DefinitionsCollection
from tfworker.handlers.collection import HandlersCollection
from tfworker.providers import ProvidersCollection


class AppState(FreezableBaseModel):
    """
    AppState defines the model for the application state. The application state is stored on the
    click context as an object that can always be retrieved by any component of the program by
    calling click.get_current_context().obj.

    The AppState contains all of the different models for different components of the application
    as well as the options loaded from the config file, and supplied via the command line. This
    allows for easy access anywhere in the application without having to overload kwargs all over
    the place.
    """

    deployment: str = Field("undefined", description="The deployment name.")
    model_config = ConfigDict(
        {
            "extra": "forbid",
            "arbitrary_types_allowed": True,
        }
    )

    authenticators: AuthenticatorsCollection | None = Field(
        None,
        description="Authenticators are what are responsible for authentication with the various backends.",
    )
    backend: BaseBackend | None = Field(
        None,
        description="The backend is responsible for interactions with the cloud provider where the remote state is stored.",
    )
    clean_options: cli_options.CLIOptionsClean | None = Field(
        None, description="These are the options passed to the clean command."
    )
    definitions: DefinitionsCollection | None = Field(
        None,
        description="Definitions are the core of a deployment, they are descriptions of local or remote terraform modules to be deployed, and how to configure them.",
    )
    handlers: HandlersCollection | None = Field(
        None,
        description="Handlers are plugins that can be executed along with terraform at various stages, they allow easily extending the application functionality.",
    )
    loaded_config: ConfigFile | None = Field(
        {},
        description="This represents the loaded configuration file, merged with various command line options.",
    )
    providers: ProvidersCollection | None = Field(
        None,
        description="Providers are terraform plugins, some provides require special handling, for example when they require authentication information, almost always the generic type is used.",
    )
    root_options: cli_options.CLIOptionsRoot | None = Field(
        None,
        description="These are the options passed to the root of the CLI, these options are focused on backend and authenticator configuration.",
    )
    terraform_options: cli_options.CLIOptionsTerraform | None = Field(
        None,
        description="These options are passed to the terraform command, they control the terraform orchestration.",
    )
    terraform_version: tuple[int, int] | None = Field(
        None,
        description="Detected terraform version as a (major, minor) tuple.",
    )
    working_dir: Path | None = Field(
        None,
        description="The working directory is the root of where all filesystem actions are handled within the application.",
    )

    def freeze(self):
        """
        Freeze the AppState and all nested models.

        This is used to prevent modification of the AppState after it has been initialized.

        the `backend` attribute is not frozen, it has no modification methods.
        """
        super().freeze()
        self.authenticators.freeze() if self.authenticators else None
        self.clean_options.freeze() if self.clean_options else None
        self.definitions.freeze() if self.definitions else None
        self.handlers.freeze() if self.handlers else None
        self.loaded_config.freeze() if self.loaded_config else None
        self.providers.freeze() if self.providers else None
        self.root_options.freeze() if self.root_options else None
        self.terraform_options.freeze() if self.terraform_options else None
        # terraform_version is a tuple, nothing to freeze
