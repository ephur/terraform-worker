from collections.abc import Mapping
from typing import Dict, TYPE_CHECKING, Union

import tfworker.util.log as log
from tfworker.exceptions import UnknownHandler, HandlerError

# @TODO: Find a better way to do this; the modules need
# imported in order to register themselves, but the whole
# point of decorating them is to try and reduce the number
# of touch points when adding a new handler
#
# they can't be imported in the registry itself due to
# circular import issues
from .trivy import TrivyHandler # noqa: F401
from .bitbucket import BitbucketHandler # noqa: F401


if TYPE_CHECKING:
    from .base import BaseHandler  # noqa: F401
    from tfworker.types import TerraformAction, TerraformStage


class HandlersCollection(Mapping):
    """
    The HandlersCollection class is a collection of handlers that are active in a various execution
    """

    def __init__(self, handlers: Dict[str, Union["BaseHandler", None]]):
        """
        Initialize the HandlersCollection object, only add handlers which have a provider key in the handlers_config dict.
        """

        self._handlers = dict()
        for k, v in handlers.items():
            log.trace(f"Adding handler {k} to handlers collection")
            log.trace(f"Handler cls: {v}")
            self._handlers[k] = v

    def __len__(self):
        return len(self._handlers)

    def __getitem__(self, value):
        if type(value) is int:
            return self._handlers[list(self._handlers.keys())[value]]
        return self._handlers[value]

    def __iter__(self):
        return iter(self._handlers.keys())

    def __setitem__(self, key, value):
        self._handlers[key] = value

    def update(self, handlers_config):
        """
        update is used to update the handlers collection with new handlers
        """
        for k in handlers_config:
            if k in self._handlers.keys():
                raise TypeError(f"Duplicate handler: {k}")
            self._handlers[k] = handlers_config[k]

    def get(self, value):
        try:
            return self[value]
        except Exception:
            raise UnknownHandler(provider=value)

    def exec_handlers(self, action: "TerraformAction", stage: "TerraformStage", **kwargs):
        """
        exec_handlers is used to execute a specific action on all handlers
        """
        from tfworker.types import TerraformAction, TerraformStage
        if action not in TerraformAction:
            raise HandlerError(f"Invalid action {action}")
        if stage not in TerraformStage:
            raise HandlerError(f"Invalid stage {stage}")
        for handler in self._handlers.values():
            if handler is not None:
                handler.exec_action(action, stage, **kwargs)
