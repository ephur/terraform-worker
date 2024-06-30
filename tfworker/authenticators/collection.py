import collections
from typing import TYPE_CHECKING

from pydantic import ValidationError

import tfworker.util.log as log

from .aws import AWSAuthenticator  # noqa
from .base import UnknownAuthenticator  # noqa
from .base import BaseAuthenticator  # noqa
from .google import GoogleAuthenticator, GoogleBetaAuthenticator  # noqa

# from tfworker.types.cli_options import CLIOptionsRoot


if TYPE_CHECKING:
    from tfworker.commands.cli_options import CLIOptionsRoot

# ALL = [AWSAuthenticator, GoogleAuthenticator, GoogleBetaAuthenticator]
ALL = [AWSAuthenticator, GoogleAuthenticator, GoogleBetaAuthenticator]


class AuthenticatorsCollection(collections.abc.Mapping):
    def __init__(self, root_args: "CLIOptionsRoot"):
        # create a collection of all authenticators that have an appropriate configuration
        # supplied for their model
        # self._authenticators = dict(
        #     [(auth.tag, auth(state_args, **kwargs)) for auth in ALL]
        # )
        self._authenticators = {}
        for auth in ALL:
            try:
                config = auth.config_model(**root_args.model_dump())
                self._authenticators[auth.tag] = auth(config)
                log.debug(f"authenticator {auth.tag} created")
            except ValidationError as e:
                log.trace(
                    f"authenticator {auth.tag} not created, configuration not supplied"
                )

    def __len__(self) -> int:
        return len(self._authenticators)

    def __getitem__(self, value) -> BaseAuthenticator:
        if type(value) is int:
            return self._authenticators[list(self._authenticators.keys())[value]]
        return self._authenticators[value]

    def __iter__(self) -> iter:
        return iter(self._authenticators.values())

    def get(self, value) -> BaseAuthenticator:
        try:
            return self[value]
        except Exception:
            raise UnknownAuthenticator(provider=value)
        return None
