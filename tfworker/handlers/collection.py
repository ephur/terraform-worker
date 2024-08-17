import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Dict, Union

import tfworker.util.log as log
from tfworker.exceptions import FrozenInstanceError, HandlerError, UnknownHandler

if TYPE_CHECKING:
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition
    from tfworker.types import TerraformAction, TerraformStage

    from .base import BaseHandler  # noqa: F401


class HandlersCollection(Mapping):
    """
    The HandlersCollection class is a collection of handlers that are active in a various execution.
    """

    _instance = None
    _lock = threading.Lock()
    _frozen: bool = False

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, handlers: Dict[str, Union["BaseHandler", None]] = None):
        """
        Initialize the HandlersCollection object, only add handlers which have a provider key in the handlers_config dict.
        """
        if not hasattr(self, "_initialized"):
            self._handlers = dict()
            if handlers:
                for k, v in handlers.items():
                    log.trace(f"Adding handler {k} to handlers collection")
                    log.trace(f"Handler cls: {v}")
                    self._handlers[k] = v
            self._initialized = True

    def __len__(self):
        return len(self._handlers)

    def __getitem__(self, value):
        if isinstance(value, int):
            return self._handlers[list(self._handlers.keys())[value]]
        return self._handlers[value]

    def __iter__(self):
        return iter(self._handlers.keys())

    def __setitem__(self, key, value):
        if self._frozen:
            raise FrozenInstanceError("Cannot modify a frozen instance.")
        self._handlers[key] = value

    def freeze(self):
        """
        freeze is used to prevent further modification of the handlers collection.
        """
        self._frozen = True

    def update(self, handlers_config):
        """
        update is used to update the handlers collection with new handlers.
        """
        for k in handlers_config:
            if k in self._handlers.keys():
                raise TypeError(f"Duplicate handler: {k}")
            self._handlers[k] = handlers_config[k]

    def get(self, value):
        try:
            return self[value]
        except KeyError:
            raise UnknownHandler(provider=value)

    def exec_handlers(
        self,
        action: "TerraformAction",
        stage: "TerraformStage",
        deployment: str,
        definition: "Definition",
        working_dir: str,
        result: Union["TerraformResult", None] = None,
    ):
        """
        exec_handlers is used to execute a specific action on all handlers.
        """
        from tfworker.types import TerraformAction, TerraformStage

        handler: BaseHandler

        if action not in TerraformAction:
            raise HandlerError(f"Invalid action {action}")
        if stage not in TerraformStage:
            raise HandlerError(f"Invalid stage {stage}")
        for name, handler in self._handlers.items():
            if handler is not None:
                if action in handler.actions and handler.is_ready():
                    log.trace(
                        f"Executing handler {name} for {definition.name} action {action} and stage {stage}"
                    )
                    handler.execute(
                        action=action,
                        stage=stage,
                        deployment=deployment,
                        definition=definition,
                        working_dir=working_dir,
                        result=result,
                    )
                else:
                    log.trace(f"Handler {name} is not ready for action {action}")
