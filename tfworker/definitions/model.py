from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

import tfworker.util.log as log


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

    name: str
    path: str
    always_apply: bool = False
    always_include: bool = False
    remote_path_options: Optional[DefinitionRemoteOptions] = None
    ignored_global_terraform_vars: Optional[List[str]] = Field(
        [], description="List of global vars to ignore."
    )
    ignored_global_remote_vars: Optional[List[str]] = Field(
        [], description="List of global remote vars to ignore."
    )
    ignored_global_template_vars: Optional[List[str]] = Field(
        [], description="List of global template vars to ignore."
    )
    use_global_terraform_vars: Optional[List[str]] = Field(
        [], description="List of global vars to use."
    )
    use_global_remote_vars: Optional[List[str]] = Field(
        [], description="List of global remote vars to use."
    )
    use_global_template_vars: Optional[List[str]] = Field(
        [], description="List of global template vars to use."
    )
    terraform_vars: Optional[Dict[str, Any]] = Field(
        {}, description="Variables to pass to terraform via a generated .tfvars file."
    )
    remote_vars: Optional[Dict[str, str]] = Field(
        {},
        description="Variables which are used to generate local references to remote state vars.",
    )
    template_vars: Optional[Dict[str, str]] = Field(
        {}, description="Variables which are suppled to any jinja templates."
    )

    # Internals, these should not be set by the user
    ready: bool = False
    needs_apply: bool = False
    plan_file: Optional[Union[str, None]] = None

    def get_target_path(self, working_dir: str) -> str:
        """
        Get the target path of the definition
        """
        return Path(f"{working_dir}/definitions/{self.path}").resolve()

    def get_template_vars(self, global_vars: Dict[str, str]) -> Dict[str, str]:
        """
        get complete template vars for the definition

        Args:
            global_vars (Dict[str, str]): the global vars to use

        Returns:
            Dict[str, str]: the complete template vars
        """
        full_vars = self.template_vars.copy()
        log.trace(f"initial template vars: {full_vars}")

        for key, value in global_vars.items():
            if key in full_vars:
                log.trace(
                    f"not adding global key: {key}, value: {value} to definition template vars, already exists"
                )
                continue
            if key in self.ignored_global_template_vars:
                log.trace(
                    f"not adding global key: {key}, value: {value} to definition template vars, ignored"
                )
                continue
            if (
                self.use_global_template_vars
                and key not in self.use_global_template_vars
            ):
                log.trace(
                    f"not adding global key: {key}, value: {value} to definition template vars, use list set, not in list"
                )
                continue
            log.trace(
                f"adding global key: {key}, value: {value} to definition template vars"
            )
            full_vars[key] = value

        return full_vars

    def get_remote_vars(self, global_vars: Dict[str, str]) -> Dict[str, str]:
        """
        get complete local to remote var mappsings for the definition

        Args:
            global_vars (Dict[str, str]): the global vars to use

        Returns:
            Dict[str, str]: the complete local vars
        """
        full_vars = self.remote_vars.copy()
        log.trace(f"initial remote vars: {full_vars}")

        for key, value in global_vars.items():
            if key in full_vars:
                log.trace(
                    f"not adding global key: {key}, value: {value} to definition remote vars, already exists"
                )
                continue
            if key in self.ignored_global_remote_vars:
                log.trace(
                    f"not adding global key: {key}, value: {value} to definition remote vars, ignored"
                )
                continue
            if self.use_global_remote_vars and key not in self.use_global_remote_vars:
                log.trace(
                    f"not adding global key: {key}, value: {value} to definition remote vars, use list set, not in list"
                )
                continue
            log.trace(
                f"adding global key: {key}, value: {value} to definition remote vars"
            )
            full_vars[key] = value

        return full_vars

    def get_terraform_vars(self, global_vars: Dict[str, str]) -> Dict[str, Any]:
        """
        get complete terraform vars for the definition

        Args:
            global_vars (Dict[str, str]): the global vars to use

        Returns:
            Dict[str, Any]: the complete terraform vars
        """
        full_vars = self.terraform_vars.copy()
        log.trace(f"initial terraform vars: {full_vars}")

        for key, value in global_vars.items():
            if key in full_vars:
                log.trace(
                    f"not adding global key: {key}, value: {value} to definition terraform vars, already exists"
                )
                continue
            if key in self.ignored_global_terraform_vars:
                log.trace(
                    f"not adding global key: {key}, value: {value} to definition terraform vars, ignored"
                )
                continue
            if (
                self.use_global_terraform_vars
                and key not in self.use_global_terraform_vars
            ):
                log.trace(
                    f"not adding global key: {key}, value: {value} to definition terraform vars, use list set, not in list"
                )
                continue
            log.trace(
                f"adding global key: {key}, value: {value} to definition terraform vars"
            )
            full_vars[key] = value

        return full_vars

    def get_used_providers(self, working_dir: str) -> Union[List[str], None]:
        """
        Get the providers used by the definition

        Args:
            working_dir (str): The working directory
            invalidate_cache (int): Passing a random int here will ensure the LRU cache is bypassed

        Returns:
            Union[List[str], None]: The list of providers used by the definition or none
        """
        return cached_get_used_providers(self.get_target_path(working_dir))


@lru_cache
def cached_get_used_providers(working_dir: str) -> Union[List[str], None]:
    """
    Get the providers used by the definition

    Args:
        working_dir (str): The working directory
        invalidate_cache (int): Passing a random int here will ensure the LRU cache is bypassed

    Returns:
        Union[List[str], None]: The list of providers used by the definition or none
    """
    from tfworker.util.terraform import find_required_providers

    try:
        return list(find_required_providers(working_dir).keys())
    except AttributeError:
        return None
