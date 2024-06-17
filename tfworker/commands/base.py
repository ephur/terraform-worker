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
import pathlib

from tfworker.authenticators import AuthenticatorsCollection
from tfworker.backends import BackendError, select_backend
from tfworker.definitions import DefinitionsCollection
from tfworker.handlers import HandlersCollection
from tfworker.handlers.exceptions import HandlerError, UnknownHandler

# from tfworker.plugins import PluginsCollection
from tfworker.providers.providers_collection import ProvidersCollection
from tfworker.util.system import get_version, which
from tfworker.util.terraform import get_terraform_version


class MissingDependencyException(Exception):
    pass


class BaseCommand:
    def __init__(self, rootc, deployment="undefined", limit=tuple(), **kwargs):
        self._rootc = rootc
        self._args_dict = dict(kwargs)
        self._args_dict.update(self._rootc.args.__dict__)

        self._version = get_version()
        self._providers = None
        self._definitions = None
        self._backend = None
        # self._plugins = None
        self._terraform_vars = dict()
        self._remote_vars = dict()
        self._temp_dir = rootc.temp_dir
        self._repository_path = rootc.args.repository_path

        rootc.add_arg("deployment", deployment)
        rootc.load_config()

        self._provider_cache = self._resolve_arg("provider_cache")
        if self._provider_cache is not None:
            self._provider_cache = pathlib.Path(self._provider_cache).resolve()

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
            ) = get_terraform_version(self._terraform_bin)

        self._authenticators = AuthenticatorsCollection(
            rootc.args, deployment=deployment, **kwargs
        )
        self._providers = ProvidersCollection(
            rootc.providers_odict, self._authenticators
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
            provider_cache=self._provider_cache,
        )
        # plugins_odict = dict()
        for provider in rootc.providers_odict:
            try:
                raw_version = rootc.providers_odict[provider]["requirements"]["version"]
            except KeyError:
                click.secho(
                    "providers must have a version constraint specified", fg="red"
                )
                raise SystemExit()
            version = raw_version.split(" ")[-1]
            vals = {"version": version}
            base_url = rootc.providers_odict[provider].get("baseURL")
            if base_url:
                vals["baseURL"] = base_url
            source = rootc.providers_odict[provider].get("source")
            if source:
                vals["source"] = source
        try:
            self._backend = select_backend(
                self._resolve_arg("backend"),
                deployment,
                self._authenticators,
                self._definitions,
            )
        except BackendError as e:
            click.secho(e, fg="red")
            click.secho(e.help, fg="red")
            raise SystemExit(1)

        # if backend_plans is requested, check if backend supports it
        self._backend_plans = self._resolve_arg("backend_plans")
        if self._backend_plans:
            if not self._backend.plan_storage:
                click.secho(
                    f"backend {self._backend.tag} does not support backend_plans",
                    fg="red",
                )
                raise SystemExit(1)

        # initialize handlers collection
        click.secho("Initializing handlers", fg="green")
        try:
            self._handlers = HandlersCollection(rootc.handlers_odict)
        except (UnknownHandler, HandlerError, TypeError) as e:
            click.secho(e, fg="red")
            raise SystemExit(1)

        # allow a backend to implement handlers as well since they already control the provider session
        if self._backend.handlers and self._backend_plans:
            self._handlers.update(self._backend.handlers)

        # list enabled handlers
        click.secho("Enabled handlers:", fg="green")
        for h in self._handlers:
            click.secho(f"  {h}", fg="green")

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

    # @property
    # def plugins(self):
    #     return self._plugins

    @property
    def temp_dir(self):
        return self._temp_dir

    @property
    def repository_path(self):
        return self._repository_path

    def _execute_handlers(self, action, stage, **kwargs):
        """Execute all ready handlers for supported actions"""
        for h in self._handlers:
            if action in h.actions and h.is_ready():
                h.execute(action, stage, **kwargs)

    def _resolve_arg(self, name):
        """Resolve argument in order of precedence:
        1) CLI argument
        2) Config file
        """
        if name in self._args_dict and self._args_dict[name] is not None:
            return self._args_dict[name]
        if name in self._rootc.worker_options_odict:
            return self._rootc.worker_options_odict[name]
        return None
