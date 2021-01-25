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

from .base import BaseBackend


class GCSBackend(BaseBackend):
    tag = "gcs"
    auth_tag = "google"

    def __init__(self, authenticators, definitions, deployment=None):
        self._authenticator = authenticators[self.auth_tag]
        self._definitions = definitions
        if deployment:
            self._deployment = deployment

    def hcl(self, name):
        state_config = []
        state_config.append('  backend "gcs" {')
        state_config.append(f'    bucket = "{self._authenticator.bucket}"')
        state_config.append(f'    prefix = "{self._authenticator.prefix}/{name}"')
        if self._authenticator.creds_path:
            state_config.append(f'    credentials = "{self._authenticator.creds_path}"')
        state_config.append("  }")
        return "\n".join(state_config)

    def data_hcl(self, exclude):
        remote_data_config = []
        # Call the iter method for explicit control of iteration order
        for definition in self._definitions.iter():
            if definition.tag == exclude:
                break
            remote_data_config.append(
                f'data "terraform_remote_state" "{definition.tag}" {{'
            )
            remote_data_config.append('  backend = "gcs"')
            remote_data_config.append("  config = {")
            remote_data_config.append(f'    bucket = "{self._authenticator.bucket}"')
            remote_data_config.append(
                f'    prefix = "{self._authenticator.prefix}/{definition.tag}"'
            )
            if self._authenticator.creds_path:
                remote_data_config.append(
                    f'    credentials = "{self._authenticator.creds_path}"'
                )
            remote_data_config.append("  }")
            remote_data_config.append("}")
        return "\n".join(remote_data_config)
