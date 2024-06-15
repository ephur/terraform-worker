from typing import Any, Dict, Optional

from pydantic import BaseModel


class Requirements(BaseModel):
    version: str
    source: Optional[str] = None


class ProviderConfig(BaseModel):
    requirements: Requirements
    vars: Optional[Dict[str, Any]] = None
    config_blocks: Optional[Dict[str, Any]] = None
