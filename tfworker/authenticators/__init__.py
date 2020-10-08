# Copyright 2020 Richard Maynard (richard.maynard@gmail.com)
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

from .aws import AWSAuthenticator  # noqa
from .base import UnknownAuthenticator  # noqa
from .google import GoogleAuthenticator  # noqa

ALL = [AWSAuthenticator, GoogleAuthenticator]


class AuthenticatorsCollection(collections.abc.Mapping):
    def __init__(self, state_args, **kwargs):
        self._authenticators = dict(
            [(auth.tag, auth(state_args, **kwargs)) for auth in ALL]
        )

    def __len__(self):
        return len(self._authenticators)

    def __getitem__(self, value):
        if type(value) == int:
            return self._authenticators[list(self._authenticators.keys())[value]]
        return self._authenticators[value]

    def __iter__(self):
        return iter(self._authenticators.values())

    def get(self, value):
        try:
            return self[value]
        except Exception:
            pass
        return None
