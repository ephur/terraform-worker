from enum import Enum
from typing import Any, Dict, List, Union


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


# https://github.com/python/typing/issues/182
JSONType = Union[str, int, float, bool, None, Dict[str, Any], List[Any]]
