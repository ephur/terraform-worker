# Copyright 2023 Richard Maynard (richard.maynard@gmail.com)
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

from tfworker.authenticators import AuthenticatorsCollection
from tfworker.commands.base import BaseCommand


class EnvCommand(BaseCommand):
    """
    The env command translates the environment configuration that is used for the worker
    into an output that can be `eval`'d by a shell. This will allow one to maintain the
    same authentication options that the worker will use when running terraform when
    executing commands against the rendered terraform definitions such as `terraform import`
    """

    def __init__(self, rootc, **kwargs):
        # Initialize the base command
        self._rootc = rootc
        self._args_dict = dict(kwargs)
        self._args_dict.update(self._rootc.args.__dict__)

        # parse the configuration
        rootc.add_arg("deployment", "env")
        rootc.load_config()

        # initialize any authenticators
        self._authenticators = AuthenticatorsCollection(rootc.args, deployment=None)

    def exec(self):
        for auth in self._authenticators:
            for k, v in auth.env().items():
                click.secho(f"export {k}={v}")
