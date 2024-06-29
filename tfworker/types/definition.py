from collections.abc import Mapping
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DefinitionRemoteOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str
    config: Dict[str, str]
    vars: Dict[str, str]


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    always_apply: bool = False
    always_include: bool = False
    remote_path_options: Optional[DefinitionRemoteOptions] = None
    ignore_global_terraform_vars: Optional[List[str]] = Field(
        [], description="List of global vars to ignore."
    )
    ignore_global_remote_vars: Optional[List[str]] = Field(
        [], description="List of global remote vars to ignore."
    )
    ignore_global_template_vars: Optional[List[str]] = Field(
        [], description="List of global template vars to ignore."
    )
    use_global_terraform_vars: Optional[List[str]] = Field(
        [], description="List of global vars to use."
    )
    use_globak_remote_vars: Optional[List[str]] = Field(
        [], description="List of global remote vars to use."
    )
    use_global_template_vars: Optional[List[str]] = Field(
        [], description="List of global template vars to use."
    )
    terraform_vars: Optional[Dict[str, str]] = Field(
        {}, description="Variables to pass to terraform via a generated .tfvars file."
    )
    remote_vars: Optional[Dict[str, str]] = Field(
        {},
        description="Variables which are used to generate local references to remote state vars.",
    )
    template_vars: Optional[Dict[str, str]] = Field(
        {}, description="Variables which are suppled to any jinja templates."
    )

    # validate that use_global* and ignore_global* do not have any overlapping values
    # @model_validator(mode="plain")
    # def check_global_vars(self):
    #     if any(x in self.ignore_global_terraform_vars for x in self.use_global_terraform_vars):
    #         raise ValueError("use_global_terraform_vars and ignore_global_terraform_vars cannot have overlapping values.")
    #     if any(x in self.ignore_global_remote_vars for x in self.use_globak_remote_vars):
    #         raise ValueError("use_globak_remote_vars and ignore_global_remote_vars cannot have overlapping values.")
    #     if any(x in self.ignore_global_template_vars for x in self.use_global_template_vars):
    #         raise ValueError("use_global_template_vars and ignore_global_template_vars cannot have overlapping values.")
