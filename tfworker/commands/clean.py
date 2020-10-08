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

from tfworker.commands.base import BaseCommand


class CleanCommand(BaseCommand):
    def __init__(self, rootc, *args, **kwargs):
        self._config = rootc.config
        self._deployment = kwargs.get("deployment")
        self._limit = kwargs.get("limit", [])
        super(CleanCommand, self).__init__(rootc, **kwargs)

    def exec(self):
        for prov in self._providers:
            prov.clean(self._deployment, self._limit)
