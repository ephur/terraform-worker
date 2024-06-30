from collections.abc import Mapping
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, GetCoreSchemaHandler, model_validator
from pydantic_core import CoreSchema, core_schema
from typing_extensions import Annotated

from tfworker.constants import (
    TF_PROVIDER_DEFAULT_HOSTNAME,
    TF_PROVIDER_DEFAULT_NAMESPACE,
)

# from tfworker.providers.base import BaseProvider


class ProviderRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    source: Optional[str] = None


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirements: ProviderRequirements
    vars: Optional[Dict[str, Any]] = None
    config_blocks: Optional[Dict[str, Any]] = None


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
    obj: "BaseProvider"

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
    from tfworker.providers.base import BaseProvider

    Provider.model_rebuild()


init_forward_refs()
