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

from tfworker.providers.base import BaseProvider
from tfworker.types.provider import ProviderConfig


class GoogleProvider(BaseProvider):
    tag = "google"
    requires_auth = True

    def __init__(self, body: ProviderConfig):
        super(GoogleProvider, self).__init__(body)

        self._authenticator = None

    def add_authenticators(self, authenticators: "AuthenticatorsCollection"):
        from tfworker.authenticators.collection import AuthenticatorsCollection

        self._authenticator = authenticators.get(self.tag)

        # if there is a creds file, tuck it into the provider vars
        if self._authenticator.creds_path:
            self.vars["credentials"] = f'file("{self._authenticator.creds_path}")'
