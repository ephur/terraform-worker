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

import re
from collections import OrderedDict

import click
from tfworker.authenticators import AuthenticatorsCollection
from tfworker.backends import select_backend
from tfworker.definitions import DefinitionsCollection
from tfworker.plugins import PluginsCollection
from tfworker.providers import ProvidersCollection
from tfworker.util.system import pipe_exec, which


class MissingDependencyException(Exception):
    pass


class BaseCommand:
    def __init__(self, rootc, deployment="undefined", limit=tuple(), **kwargs):
        self._rootc = rootc
        self._args_dict = dict(kwargs)
        self._args_dict.update(self._rootc.args.__dict__)

        self._version = None
        self._providers = None
        self._definitions = None
        self._backend = None
        self._plugins = None
        self._terraform_vars = OrderedDict()
        self._remote_vars = OrderedDict()
        self._temp_dir = rootc.temp_dir
        self._repository_path = rootc.args.repository_path

        rootc.add_arg("deployment", deployment)
        rootc.load_config()

        (self._tf_version_major, self._tf_version_minor) = self._resolve_arg(
            "tf_version"
        ) or (None, None)

        self._terraform_bin = self._resolve_arg("terraform_bin") or which("terraform")
        if not self._terraform_bin:
            raise MissingDependencyException(
                "Cannot find terraform in arguments or on PATH"
            )
        if self._tf_version_major is None or self._tf_version_minor is None:
            (
                self._tf_version_major,
                self._tf_version_minor,
            ) = self.get_terraform_version(self._terraform_bin)

        self._authenticators = AuthenticatorsCollection(
            rootc.args, deployment=deployment, **kwargs
        )

        rootc.clean = kwargs.get("clean", True)

        self._providers = ProvidersCollection(
            rootc.providers_odict, self._authenticators, self._tf_version_major
        )
        self._plan_for = "destroy" if self._resolve_arg("destroy") else "apply"
        self._definitions = DefinitionsCollection(
            rootc.definitions_odict,
            deployment,
            limit,
            self._plan_for,
            self._providers,
            self._repository_path,
            rootc,
            self._temp_dir,
            self._tf_version_major,
        )
        plugins_odict = OrderedDict()
        for provider in rootc.providers_odict:
            raw_version = rootc.providers_odict[provider]["vars"]["version"]
            version = raw_version.split(" ")[-1]
            vals = {"version": version}
            base_url = rootc.providers_odict[provider].get("baseURL")
            if base_url:
                vals["baseURL"] = base_url
            source = rootc.providers_odict[provider].get("source")
            if source:
                vals["source"] = source
            plugins_odict[str(provider)] = vals
        self._plugins = PluginsCollection(
            plugins_odict, self._temp_dir, self._tf_version_major
        )
        self._backend = select_backend(
            self._resolve_arg("backend"),
            deployment,
            self._authenticators,
            self._definitions,
        )

    @property
    def authenticators(self):
        return self._authenticators

    @property
    def backend(self):
        return self._backend

    @property
    def providers(self):
        return self._providers

    @property
    def definitions(self):
        return self._definitions

    @property
    def plugins(self):
        return self._plugins

    @property
    def temp_dir(self):
        return self._temp_dir

    @property
    def repository_path(self):
        return self._repository_path

    def _resolve_arg(self, name):
        """Resolve argument in order of precedence:
        1) CLI argument
        2) Config file
        """
        if name in self._args_dict and self._args_dict[name]:
            return self._args_dict[name]
        if name in self._rootc.worker_options_odict:
            return self._rootc.worker_options_odict[name]
        return None

    @staticmethod
    def get_terraform_version(terraform_bin):
        (return_code, stdout, stderr) = pipe_exec(f"{terraform_bin} version")
        if return_code != 0:
            click.secho(f"unable to get terraform version\n{stderr}", fg="red")
            raise SystemExit(1)
        version = stdout.decode("UTF-8").split("\n")[0]
        version_search = re.search(r".* v\d+\.(\d+)\.(\d+)", version)
        if version_search:
            click.secho(
                f"Terraform Version Result: {version}, using major:{version_search.group(1)}, minor:{version_search.group(2)}",
                fg="yellow",
            )
            return (int(version_search.group(1)), int(version_search.group(2)))
        else:
            click.secho(f"unable to get terraform version\n{stderr}", fg="red")
            raise SystemExit(1)
