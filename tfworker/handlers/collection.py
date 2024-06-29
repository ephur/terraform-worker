# Use the registry instead of the collection to register handlers

from collections.abc import Mapping
from typing import Dict, KeysView

import tfworker.util.log as log
from tfworker.exceptions import UnknownHandler

# Make all of the handlers available from the handlers module
from .base import BaseHandler  # noqa: F401
from .bitbucket import BitbucketHandler  # noqa: F401
from .trivy import TrivyHandler  # noqa: F401


class HandlersCollection(Mapping):
    """
    The HandlersCollection class is a collection of handlers. It is meant to be used as a singleton.
    """

    def __init__(self, handlers: Dict[str, BaseHandler | None]):
        """
        Initialize the HandlersCollection object, only add handlers which have a provider key in the handlers_config dict.
        """
        self._handlers = dict()

        # for k in handlers_config:
        #     if k in self._handlers.keys():
        #         raise TypeError(f"Duplicate handler: {k}")
        #     if k == "bitbucket":
        #         self._handlers["bitbucket"] = BitbucketHandler(handlers_config[k])
        #     elif k == "trivy":
        #         self._handlers["trivy"] = TrivyHandler(handlers_config[k])
        #     else:
        #         raise UnknownHandler(provider=k)
        for k, v in handlers.items():
            log.debug(f"Adding handler {k} to handlers collection")
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
