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


class BaseAuthenticator:
    tag = "base"

    def __init__(self, state_args, **kwargs):
        self._args = state_args
        self.clean = kwargs.get("clean")
        self.create_backend_bucket = self._resolve_arg("create_backend_bucket")

    def _resolve_arg(self, name):
        return getattr(self._args, name) if hasattr(self._args, name) else None

    def env(self):
        return {}


class UnknownAuthenticator(Exception):
    def __init__(self, provider):
        super().__init__(f"{provider} is not a known value.")
