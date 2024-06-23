from enum import Enum
from typing import Optional

from pydantic import BaseModel

from tfworker.constants import (
    TF_PROVIDER_DEFAULT_HOSTNAME,
    TF_PROVIDER_DEFAULT_NAMESPACE,
)


class TerraformAction(Enum):
    """
    Terraform actions
    """

    PLAN = "plan"
    APPLY = "apply"
    DESTROY = "destroy"
    INIT = "init"

    def __str__(self):
        return self.value


class TerraformStage(Enum):
    """
    Stages around a terraform action, pre and post
    """

    PRE = "pre"
    POST = "post"

    def __str__(self):
        return self.value
