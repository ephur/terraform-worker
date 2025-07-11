from abc import ABCMeta, abstractmethod
from typing import TYPE_CHECKING, Union

from pydantic import BaseModel

if TYPE_CHECKING:
    from tfworker.commands.terraform import TerraformResult
    from tfworker.custom_types import TerraformAction, TerraformStage
    from tfworker.definitions.model import Definition


class BaseConfig(BaseModel): ...  # noqa


class BaseHandler(metaclass=ABCMeta):
    """The base handler class should be extended by all handler classes."""

    actions = []
    config_model = BaseConfig
    _ready = False
    # default_priority can be overridden by subclasses to influence ordering
    # when handlers are executed. lower values indicate higher priority.
    default_priority: dict = {}
    # dependencies allow a handler to specify other handlers that must run
    # before it for a given action and stage. Format:
    # {TerraformAction: {TerraformStage: ["handler_name", ...]}}
    dependencies: dict = {}

    @abstractmethod
    def __init__(self, config: BaseModel) -> None:
        pass

    def is_ready(self) -> bool:  # pragma: no cover
        """is_ready is called to determine if a handler is ready to be executed"""
        try:
            return self._ready
        except AttributeError:
            return False

    @abstractmethod
    def execute(
        self,
        action: "TerraformAction",
        stage: "TerraformStage",
        deployment: str,
        definition: "Definition",
        working_dir: str,
        result: Union["TerraformResult", None] = None,
    ) -> None:  # pragma: no cover
        """
        execute is called when a handler should trigger, it accepts to parameters
            action: the action that triggered the handler (one of plan, clean, apply, destroy)
            stage: the stage of the action (one of pre, post)
            kwargs: any additional arguments that may be required
        """
        pass

    def __str__(self):
        return self.__class__.__name__
