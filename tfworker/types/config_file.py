from typing import Any, Dict, Optional

from pydantic import ConfigDict, Field

from .freezable_basemodel import FreezableBaseModel


class GlobalVars(FreezableBaseModel):
    """
    Global Variables can be defined inside of the configuration file, this is a model for those variables.
    """

    model_config = ConfigDict(extra="forbid")

    terraform_vars: Dict[str, str | bool] = Field(
        {}, description="Variables to pass to terraform via a generated .tfvars file."
    )
    remote_vars: Dict[str, str | bool] = Field(
        {},
        description="Variables which are used to generate local references to remote state vars.",
    )
    template_vars: Dict[str, str | bool] = Field(
        {}, description="Variables which are suppled to any jinja templates."
    )


class ConfigFile(FreezableBaseModel):
    """
    This model is used to validate and deserialize the configuration file.
    """

    model_config = ConfigDict(extra="forbid")

    definitions: Dict[str, Any] = Field(
        {}, description="The definition configurations."
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

    def freeze(self):
        super().freeze()
        if self.global_vars:
            self.global_vars.freeze()
        return self
