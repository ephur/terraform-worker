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

import click

from tfworker.commands.base import BaseCommand
from tfworker.exceptions import BackendError


class CleanCommand(BaseCommand):
    def __init__(self, rootc, **kwargs):
        # super(CleanCommand, self).__init__(rootc, **kwargs)
        # self._deployment = self._resolve_arg("deployment")
        # self._limit = self._resolve_arg("limit")
        pass

    def exec(self):
        # try:
        #     self._backend.clean(deployment=self._deployment, limit=self._limit)
        # except BackendError as e:
        #     click.secho(f"error while cleaning: {e}", fg="red")
        #     raise SystemExit(1)
        # click.secho("backend cleaning completed", fg="green")
        pass
