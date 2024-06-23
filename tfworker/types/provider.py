from typing import Any, Dict, Optional

from pydantic import BaseModel

from tfworker.constants import (
    TF_PROVIDER_DEFAULT_HOSTNAME,
    TF_PROVIDER_DEFAULT_NAMESPACE,
)


class ProviderRequirements(BaseModel):
    version: str
    source: Optional[str] = None


class ProviderConfig(BaseModel):
    requirements: ProviderRequirements
    vars: Optional[Dict[str, Any]] = None
    config_blocks: Optional[Dict[str, Any]] = None


class ProviderGID(BaseModel):
    """
    The provider global identifier
    """

    hostname: Optional[str] = TF_PROVIDER_DEFAULT_HOSTNAME
    namespace: Optional[str] = TF_PROVIDER_DEFAULT_NAMESPACE
    type: str

    def __str__(self):
        return f"{self.hostname}/{self.namespace}/{self.type}"
