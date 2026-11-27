from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from tfworker.definitions.model import DefinitionRemoteOptions

from .freezable_basemodel import FreezableBaseModel


class ParallelOptions(FreezableBaseModel):
    """
    Configuration options for parallel processing of definitions.
    """

    model_config = ConfigDict(extra="forbid")

    max_preparation_workers: int = Field(
        8,
        description="Maximum number of parallel workers for file preparation operations",
    )
    max_init_workers: int = Field(
        4,
        description="Maximum number of parallel workers for terraform init operations",
    )


class GlobalVars(FreezableBaseModel):
    """
    Global Variables can be defined inside of the configuration file, this is a model for those variables.
    """

    model_config = ConfigDict(extra="forbid")

    terraform_vars: Dict[str, str | bool | list | dict] = Field(
        {}, description="Variables to pass to terraform via a generated .tfvars file."
    )
    remote_vars: Dict[str, str | bool | list | dict] = Field(
        {},
        description="Variables which are used to generate local references to remote state vars.",
    )
    template_vars: Dict[str, str | bool | list | dict] = Field(
        {}, description="Variables which are suppled to any jinja templates."
    )


class DefinitionsSource(BaseModel):
    """
    A source fetched like a definition whose command prints more definitions.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="The local path or git URL of the source.")
    remote_path_options: Optional[DefinitionRemoteOptions] = Field(
        None, description="Options for fetching a remote source."
    )
    command: str = Field(
        description="Command run in the fetched copy; prints definitions as YAML or JSON."
    )
    after: Optional[str] = Field(
        None,
        description="Insert generated definitions after this definition; appended when unset.",
    )


class ConfigFile(FreezableBaseModel):
    """
    This model is used to validate and deserialize the configuration file.
    """

    model_config = ConfigDict(extra="forbid")

    definitions: Dict[str, Any] = Field(
        {}, description="The definition configurations."
    )
    definitions_sources: Dict[str, DefinitionsSource] = Field(
        {}, description="Sources whose commands generate definitions."
    )
    global_vars: Optional[GlobalVars] = Field(
        default_factory=GlobalVars,
        description="Global variables that are used in the configuration file.",
    )
    providers: Dict[str, Any] = Field({}, description="The provider configurations.")
    worker_options: Dict[str, str | bool] = Field(
        {}, description="The base worker options, overlaps with command line options"
    )
    handlers: Dict[str, Any] = Field({}, description="The handler configurations.")
    parallel_options: Optional[ParallelOptions] = Field(
        default_factory=ParallelOptions,
        description="Configuration options for parallel processing of definitions.",
    )

    def freeze(self):
        super().freeze()
        if self.global_vars:
            self.global_vars.freeze()
        if self.parallel_options:
            self.parallel_options.freeze()
        return self
