from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tfworker.constants import (
    TF_PROVIDER_DEFAULT_HOSTNAME,
    TF_PROVIDER_DEFAULT_NAMESPACE,
)


class ProviderRequirements(BaseModel):
    """
    Represents the requirements for a provider, such as the version and source.
    """

    model_config = ConfigDict(extra="forbid")

    version: str = Field(description='Version constraint, such as ">= 5.0".')
    source: Optional[str] = Field(
        None,
        description="Provider source address; defaults to hashicorp/<name> on the public registry.",
    )


class ProviderAlias(BaseModel):
    """
    Represents an alias in the provider configuration, such as an AWS region alias.
    """

    model_config = ConfigDict(extra="forbid")

    vars: Optional[Dict[str, Any]] = Field(
        None, description="Arguments that replace the provider's vars for this alias."
    )
    config_blocks: Optional[Dict[str, Any]] = Field(
        None,
        description="Nested blocks that replace the provider's config_blocks for this alias.",
    )


class ProviderConfig(BaseModel):
    """
    Represents the configuration for a provider, such as the requirements, variables, and config blocks.
    """

    model_config = ConfigDict(extra="forbid")

    requirements: ProviderRequirements = Field(
        description="Version and source written to required_providers."
    )
    vars: Optional[Dict[str, Any]] = Field(
        None, description="Arguments written into the provider block."
    )
    config_blocks: Optional[Dict[str, Any]] = Field(
        None,
        description="Nested blocks written into the provider block, such as assume_role.",
    )
    aliases: Optional[Dict[str, ProviderAlias]] = Field(
        None, description="Additional aliased provider blocks, keyed by alias."
    )

    @model_validator(mode="before")
    @classmethod
    def merge_aliases(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """
        Merges alias vars and config_blocks, overriding top-level vars and config_blocks
        for each alias.
        """
        aliases = values.get("aliases", {})

        # No aliases, no merging needed
        if not aliases:
            return values

        top_level_vars = values.get("vars", {}) or {}
        top_level_config_blocks = values.get("config_blocks", {}) or {}

        # Process each alias, note we are still working with dictionaries, not Pydantic models yet
        for alias_name, alias_config in aliases.items():
            alias_vars = alias_config.get("vars", {})  # Access as a dict
            alias_config_blocks = alias_config.get(
                "config_blocks", {}
            )  # Access as a dict

            # The alias vars should override top-level vars
            merged_vars = {**top_level_vars, **alias_vars}

            # The alias config_blocks should override top-level config_blocks
            merged_config_blocks = {**top_level_config_blocks, **alias_config_blocks}

            # Apply the merged vars and config_blocks to the alias
            alias_config["vars"] = merged_vars
            alias_config["config_blocks"] = merged_config_blocks

        # Update values with the merged aliases (still as dicts)
        values["aliases"] = aliases
        return values


class ProviderGID(BaseModel):
    """
    The provider global identifier
    """

    model_config = ConfigDict(extra="forbid")

    hostname: Optional[str] = TF_PROVIDER_DEFAULT_HOSTNAME
    namespace: Optional[str] = TF_PROVIDER_DEFAULT_NAMESPACE
    type: str

    def __str__(self):
        return f"{self.hostname}/{self.namespace}/{self.type}"


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_arbitrary_types=True)

    name: str
    gid: ProviderGID
    config: ProviderConfig
    obj: "BaseProvider"  # noqa: F821

    # When the model is created, the gid is created from requirements.source, or the name
    @model_validator(mode="before")
    @classmethod
    def create_gid(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        if v.get("gid", None) is not None:
            print("GID already exists")
            return v
        if v.get("config", None) is None:
            raise ValueError("config is required for provider")

        if v["config"].requirements.source is None:
            v["gid"] = ProviderGID(type=v["name"])
            return v

        # parse the source to get the hostname, namespace, and type
        gid_parts = v["config"].requirements.source.split("/")
        if len(gid_parts) > 4:
            raise ValueError(
                f"Invalid source for provider {v['name']}: {v.config.source}"
            )
        if len(gid_parts) == 3:
            v["gid"] = ProviderGID(
                hostname=gid_parts[0], namespace=gid_parts[1], type=gid_parts[2]
            )
        elif len(gid_parts) == 2:
            v["gid"] = ProviderGID(namespace=gid_parts[0], type=gid_parts[1])
        else:
            v["gid"] = ProviderGID(type=gid_parts[0])

        return v

    def __str__(self):
        return self.name


def init_forward_refs():
    # this is required to prevent circular imports
    from tfworker.providers.base import BaseProvider  # noqa: F401

    Provider.model_rebuild()


init_forward_refs()
