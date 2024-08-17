from abc import ABCMeta, abstractmethod
from typing import TYPE_CHECKING, Union

from pydantic import BaseModel

if TYPE_CHECKING:
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition
    from tfworker.types import TerraformAction, TerraformStage


class BaseConfig(BaseModel): ...  # noqa


class BaseHandler(metaclass=ABCMeta):
    """The base handler class should be extended by all handler classes."""

    actions = []
    config_model = BaseConfig
    _ready = False

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
