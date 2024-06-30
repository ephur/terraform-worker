from collections.abc import Mapping
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DefinitionRemoteOptions(BaseModel):
    """
    Model to define the remote_options of a definition
    """

    model_config = ConfigDict(extra="forbid")

    backend: str
    config: Dict[str, str]
    vars: Dict[str, str]


class Definition(BaseModel):
    """
    Model to define a definition
    """

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
