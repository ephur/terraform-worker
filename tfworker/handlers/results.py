from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from tfworker.custom_types.terraform import TerraformAction, TerraformStage


class BaseHandlerResult(BaseModel):
    """Base result model for handler outputs."""

    handler: str
    action: TerraformAction
    stage: TerraformStage
    data: dict[str, Any] | None = None

    model_config = {
        "extra": "allow",
    }
