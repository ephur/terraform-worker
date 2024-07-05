import collections
import threading
from typing import TYPE_CHECKING

from pydantic import ValidationError

import tfworker.util.log as log
from tfworker.exceptions import UnknownAuthenticator

from .aws import AWSAuthenticator  # noqa
from .base import BaseAuthenticator  # noqa
from .google import GoogleAuthenticator, GoogleBetaAuthenticator  # noqa

if TYPE_CHECKING:
    from tfworker.cli_options import CLIOptionsRoot  # pragma: no cover  # noqa

ALL = [AWSAuthenticator, GoogleAuthenticator, GoogleBetaAuthenticator]


class AuthenticatorsCollection(collections.abc.Mapping):
    """
    A thread safe, singleton collection of all authenticators that have an appropriate configuration

    Attributes:
        _instance (AuthenticatorsCollection): The singleton instance of the collection
        _lock (threading.Lock): A lock to ensure thread safety
        _authenticators (dict): The collection of authenticators
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, root_args: "CLIOptionsRoot"):
        if not hasattr(self, "_initialized"):
            self._authenticators = {}
            for auth in ALL:
                try:
                    config = auth.config_model(**root_args.model_dump())
                    self._authenticators[auth.tag] = auth(config)
                    log.debug(f"authenticator {auth.tag} created")
                except ValidationError:
                    log.trace(
                        f"authenticator {auth.tag} not created, configuration not supplied"
                    )
            self._initialized = True

    def __len__(self) -> int:
        return len(self._authenticators)

    def __getitem__(self, value) -> BaseAuthenticator:
        try:
            if isinstance(value, int):
                return self._authenticators[list(self._authenticators.keys())[value]]
            return self._authenticators[value]
        except KeyError:
            raise UnknownAuthenticator(provider=value)

    def __iter__(self) -> iter:
        return iter(self._authenticators.values())

    def get(self, value) -> BaseAuthenticator:
        return self[value]
