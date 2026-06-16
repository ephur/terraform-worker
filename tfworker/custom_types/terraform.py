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
    Stages around a terraform action: pre, post, and error.

    ERROR is dispatched to handlers in place of POST when a terraform action
    fails, so handlers can react to failures (e.g. record a failed status, or
    avoid persisting partial state) without changing existing POST behavior.
    Hook scripts are not invoked for the ERROR stage.
    """

    PRE = "pre"
    POST = "post"
    ERROR = "error"

    def __str__(self):
        return self.value
