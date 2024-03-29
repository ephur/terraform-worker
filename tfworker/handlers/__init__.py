import collections

from .bitbucket import BitbucketHandler
from .exceptions import HandlerError, UnknownHandler


class HandlersCollection(collections.abc.Mapping):
    """
    The HandlersCollection class is a collection of handlers. It is meant to be used as a singleton.
    """

    def __init__(self, handlers_config):
        """
        Initialize the HandlersCollection object, only add handlers which have a provider key in the handlers_config dict.
        """
        self._handlers = dict()

        for k in handlers_config:
            if k in self._handlers.keys():
                raise TypeError(f"Duplicate handler: {k}")
            if k == "bitbucket":
                self._handlers["bitbucket"] = BitbucketHandler(handlers_config[k])
            else:
                raise UnknownHandler(provider=k)

    def __len__(self):
        return len(self._handlers)

    def __getitem__(self, value):
        if type(value) == int:
            return self._handlers[list(self._handlers.keys())[value]]
        return self._handlers[value]

    def __iter__(self):
        return iter(self._handlers.values())

    def get(self, value):
        try:
            return self[value]
        except Exception:
            raise UnknownHandler(provider=value)
        return None
