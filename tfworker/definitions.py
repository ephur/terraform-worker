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
import copy
import json
from pathlib import Path, PurePath
from typing import OrderedDict

import click
import hcl2
from tfworker import constants as const
from tfworker.util.copier import CopyFactory

TERRAFORM_TPL = """\
terraform {{
{0}
}}
"""


class ReservedFileError(Exception):
    pass


class Definition:
    def __init__(
        self,
        definition,
        body,
        deployment,
        global_remote_vars,
        global_template_vars,
        global_terraform_vars,
        providers,
        repository_path,
        temp_dir,
        tf_version_major,
        limited=False,
    ):
        self.tag = definition
        self._body = body
        self._path = body.get("path")
        self._remote_vars = self.make_vars(
            body.get("remote_vars", collections.OrderedDict()), global_remote_vars
        )
        self._terraform_vars = self.make_vars(
            body.get("terraform_vars", collections.OrderedDict()), global_terraform_vars
        )

        self._deployment = deployment
        self._repository_path = repository_path
        self._providers = providers
        self._temp_dir = temp_dir
        self._tf_version_major = tf_version_major
        self._limited = limited

        self._target = f"{self._temp_dir}/definitions/{self.tag}".replace("//", "/")

    @property
    def body(self):
        return self._body

    @property
    def limited(self):
        return self._limited

    @property
    def path(self):
        return self._path

    @property
    def provider_names(self):
        """ Extract only the providers used by a definition """
        result = set(self._providers.keys())
        if self._tf_version_major >= 13:
            version_path = PurePath(self._target) / "versions.tf"
            if Path(version_path).exists():
                with open(version_path, "r") as reader:
                    vinfo = hcl2.load(reader)
                    tf_element = vinfo.get("terraform", [None]).pop()
                    if tf_element:
                        rp_element = tf_element.get("required_providers", [None]).pop()
                        if rp_element:
                            required_providers = set(rp_element.keys())
                            result = result.intersection(required_providers)
        return result

    def prep(self, backend):
        """ prepare the definitions for running """

        try:
            c = CopyFactory.create(
                self.path,
                root_path=self._repository_path,
                conflicts=const.RESERVED_FILES,
            )
        except NotImplementedError:
            click.secho(
                f"could not handle source path {self.path} for definition {self.tag}, either file does not exist or could not handle remote URI",
                fg="red",
            )
            raise SystemExit(1)

        remote_options = dict(self.body.get("remote_path_options", {}))

        try:
            c.copy(destination=self._target, **remote_options)
        except FileExistsError as e:
            raise ReservedFileError(e)

        # Create local vars from remote data sources
        if len(list(self._remote_vars.keys())) > 0:
            with open(f"{self._target}/worker-locals.tf", "w+") as tflocals:
                tflocals.write("locals {\n")
                for k, v in self._remote_vars.items():
                    tflocals.write(f"  {k} = data.terraform_remote_state.{v}\n")
                tflocals.write("}\n\n")

        # create remote data sources, and required providers
        remotes = list(map(lambda x: x.split(".")[0], self._remote_vars.values()))
        with open(f"{self._target}/terraform.tf", "w+") as tffile:
            tffile.write(f"{self._providers.hcl(self.provider_names)}\n\n")
            tffile.write(TERRAFORM_TPL.format(f"{backend.hcl(self.tag)}"))
            tffile.write(backend.data_hcl(remotes))

        # Create the variable definitions
        with open(f"{self._target}/worker.auto.tfvars", "w+") as varfile:
            for k, v in self._terraform_vars.items():
                varfile.write(f"{k} = {self.vars_typer(v)}\n")

    @staticmethod
    def quote_str(some_string):
        """Put literal quotes around a string."""
        return f'"{some_string}"'

    def make_vars(self, local_vars, global_vars):
        """Make a variables dictionary based on default vars, as well as specific vars for an item."""
        global_vars = global_vars or collections.OrderedDict()
        item_vars = copy.deepcopy(global_vars)
        for k, v in local_vars.items():
            item_vars[k] = v
        return item_vars

    @staticmethod
    def vars_typer(v, inner=False):
        """
        vars_typer is used to assemble variables as they are parsed from the yaml configuration
        into the required format to be used in terraform
        """
        if v is True:
            return "true"
        elif v is False:
            return "false"
        elif isinstance(v, list):
            rval = []
            for val in v:
                result = Definition.vars_typer(val, inner=True)
                try:
                    rval.append(result.strip('"').strip("'"))
                except AttributeError:
                    rval.append(result)
            if inner:
                return rval
            else:
                return json.dumps(rval)
        elif isinstance(v, OrderedDict) or isinstance(v, dict):
            rval = {}
            for k, val in v.items():
                result = Definition.vars_typer(val, inner=True)
                try:
                    rval[k] = result.strip('"').strip("'")
                except AttributeError:
                    rval[k] = result
            if inner:
                return rval
            else:
                return json.dumps(rval)
        return f'"{v}"'


class DefinitionsCollection(collections.abc.Mapping):
    def __init__(
        self,
        definitions,
        deployment,
        limit,
        plan_for,
        providers,
        repository_path,
        rootc,
        temp_dir,
        tf_version_major,
    ):
        self._body = definitions
        self._plan_for = plan_for
        self._definitions = collections.OrderedDict()
        self._limit = True if len(limit) > 0 else False
        self._limit_size = len(limit)
        for definition, body in definitions.items():
            self._definitions[definition] = Definition(
                definition,
                body,
                deployment,
                rootc.remote_vars_odict,
                rootc.template_vars_odict,
                rootc.terraform_vars_odict,
                providers,
                repository_path,
                temp_dir,
                tf_version_major,
                True if limit and definition in limit else False,
            )

    def __len__(self):
        return len(self._definitions)

    def __getitem__(self, value):
        if type(value) == int:
            return self._definitions[list(self._definitions.keys())[value]]
        return self._definitions[value]

    def __iter__(self):
        return self.iter(honor_destroy=True)

    def iter(self, honor_destroy=False):
        if honor_destroy:
            if self._plan_for == "destroy":
                return iter(reversed(list(self._definitions.values())))
        return iter(self._definitions.values())

    def limited(self):
        # handle the case where nothing is filtered
        iter_size = len(
            list(filter(lambda d: d.limited, self.iter(honor_destroy=True)))
        )
        if iter_size == 0:
            # a limit was supplied, but not matched, raise an error
            if self._limit:
                raise ValueError("no definitions matching --limit")
            # the run is not limited to anything, so return everything
            else:
                return self.iter(honor_destroy=True)
        elif iter_size < self._limit_size:
            # not all limit items are matched
            raise ValueError("not all definitions match --limit")
        else:
            return iter(filter(lambda d: d.limited, self.iter(honor_destroy=True)))

    @property
    def body(self):
        return self._body
