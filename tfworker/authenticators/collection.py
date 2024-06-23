# Copyright 2020-2023 Richard Maynard (richard.maynard@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections

from pydantic import ValidationError

import tfworker.util.log as log
from tfworker.types.cli_options import CLIOptionsRoot

from .aws import AWSAuthenticator  # noqa
from .base import UnknownAuthenticator  # noqa
from .base import BaseAuthenticator, BaseAuthenticatorConfig
from .google import GoogleAuthenticator, GoogleBetaAuthenticator  # noqa

# ALL = [AWSAuthenticator, GoogleAuthenticator, GoogleBetaAuthenticator]
ALL = [AWSAuthenticator, GoogleAuthenticator, GoogleBetaAuthenticator]


class AuthenticatorsCollection(collections.abc.Mapping):
    def __init__(self, root_args: CLIOptionsRoot):
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
                log.debug(
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
