from pathlib import Path
from typing import Any, Dict

from pydantic import BaseModel, ConfigDict, Field

from tfworker.authenticators.collection import AuthenticatorsCollection
from tfworker.backends.base import BaseBackend
from tfworker.definitions import DefinitionsCollection
from tfworker.handlers.collection import HandlersCollection
from tfworker.providers.collection import ProvidersCollection

from . import cli_options
from .config_file import ConfigFile


class AppState(BaseModel):
    deployment: str = Field("undefined", description="The deployment name.")
    model_config = ConfigDict(
        {
            "extra": "forbid",
            "arbitrary_types_allowed": True,
        }
    )

    root_options: cli_options.CLIOptionsRoot | None = Field(
        None, description="The root options."
    )
    clean_options: cli_options.CLIOptionsClean | None = Field(
        None, description="The clean options."
    )
    terraform_options: cli_options.CLIOptionsTerraform | None = Field(
        None, description="The terraform options."
    )
    loaded_config: ConfigFile | None = Field(
        {}, description="The loaded configuration file."
    )
    working_dir: Path | None = Field(None, description="The working directory.")
    providers: ProvidersCollection | None = Field(
        None, description="The provider configurations."
    )
    authenticators: AuthenticatorsCollection | None = Field(
        None, description="The authenticator configurations."
    )
    definitions: DefinitionsCollection | None = Field(
        None, description="The definition configurations."
    )
    backend: BaseBackend | None = Field(None, description="The backend configuration.")
    handlers: HandlersCollection | None = Field(None, description="The handlers.")
