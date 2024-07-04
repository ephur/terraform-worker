from enum import Enum


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
