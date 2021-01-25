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
import os
import shutil
import sys
from pathlib import Path

import click
import jinja2
from tfworker import constants as const

TERRAFORM_TPL = """\
terraform {{
{0}

{1}
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
        limited=False,
    ):
        self.tag = definition
        self._body = body
        self._path = body.get("path")
        self._remote_vars = self.make_vars(
            body.get("remote_vars", collections.OrderedDict()), global_remote_vars
        )
        self._template_vars = self.make_vars(
            body.get("template_vars", collections.OrderedDict()), global_template_vars
        )
        self._terraform_vars = self.make_vars(
            body.get("terraform_vars", collections.OrderedDict()), global_terraform_vars
        )

        self._deployment = deployment
        self._repository_path = repository_path
        self._providers = providers
        self._temp_dir = temp_dir
        self._limited = limited

    @property
    def body(self):
        return self._body

    @property
    def limited(self):
        return self._limited

    @property
    def path(self):
        return self._path

    def prep(self, backend):
        """ prepare the definitions for running """
        repo = Path(f"{self._repository_path}/{self.path}".replace("//", "/"))
        target = Path(f"{self._temp_dir}/definitions/{self.tag}".replace("//", "/"))
        target.mkdir(parents=True, exist_ok=True)

        if not repo.exists():
            click.secho(
                f"Error preparing definition {self.tag}, path {repo.resolve()} does not"
                " exist",
                fg="red",
            )
            sys.exit(1)

        # Prepare variables
        self._template_vars["deployment"] = self._deployment
        self._terraform_vars["deployment"] = self._deployment

        # Put terraform files in place
        for tf in repo.glob("*.tf"):
            if tf.name in const.RESERVED_FILES:
                raise ReservedFileError(f"{tf} is not allowed")
            shutil.copy(str(tf), str(target))

        for tf in repo.glob("*.tfvars"):
            shutil.copy(str(tf), str(target))

        if os.path.isdir(f"{repo}/templates".replace("//", "/")):
            shutil.copytree(f"{repo}/templates", f"{target}/templates")

        if os.path.isdir(f"{repo}/policies".replace("//", "/")):
            shutil.copytree(f"{repo}/policies", f"{target}/policies")

        if os.path.isdir(f"{repo}/scripts".replace("//", "/")):
            shutil.copytree(f"{repo}/scripts", f"{target}/scripts")

        if os.path.isdir(f"{repo}/hooks".replace("//", "/")):
            shutil.copytree(f"{repo}/hooks", f"{target}/hooks")

        if os.path.isdir(f"{repo}/repos".replace("//", "/")):
            shutil.copytree(f"{repo}/repos", f"{target}/repos")

        # Render jinja templates and put in place
        env = jinja2.Environment(loader=jinja2.FileSystemLoader)

        for j2 in repo.glob("*.j2"):
            contents = env.get_template(str(j2)).render(**self._template_vars)
            with open(f"{target}/{j2}", "w+") as j2_file:
                j2_file.write(contents)

        # Create local vars from remote data sources
        if len(list(self._remote_vars.keys())) > 0:
            with open(f"{target}/worker-locals.tf", "w+") as tflocals:
                tflocals.write("locals {\n")
                for k, v in self._remote_vars.items():
                    tflocals.write(f"  {k} = data.terraform_remote_state.{v}\n")
                tflocals.write("}\n\n")

        with open(f"{target}/terraform.tf", "w+") as tffile:
            tffile.write(f"{self._providers.hcl()}\n\n")
            tffile.write(
                TERRAFORM_TPL.format(
                    f"{backend.hcl(self.tag)}",
                    f"{self._providers.required_providers()}",
                )
            )
            tffile.write(backend.data_hcl(self.tag))

        # Create the variable definitions
        with open(f"{target}/worker.auto.tfvars", "w+") as varfile:
            for k, v in self._terraform_vars.items():
                if isinstance(v, list):
                    varstring = f'[{", ".join(map(Definition.quote_str, v))}]'
                    varfile.write(f"{k} = {varstring}\n")
                else:
                    varfile.write(f'{k} = "{v}"\n')

    @staticmethod
    def quote_str(some_string):
        """Put literal quotes around a string."""
        return f'"{some_string}"'

    def make_vars(self, local_vars, global_vars):
        """Make a variables dictionary based on default vars, as well as specific vars for an item."""
        global_vars = global_vars or collections.OrderedDict()
        item_vars = copy.deepcopy(global_vars)
        for k, v in local_vars.items():
            # terraform expects variables in a specific type, so need to convert bools to a lower case true/false
            matched_type = False
            if v is True:
                item_vars[k] = "true"
                matched_type = True
            if v is False:
                item_vars[k] = "false"
                matched_type = True
            if not matched_type:
                item_vars[k] = v

        return item_vars


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
    ):
        self._body = definitions
        self._plan_for = plan_for
        self._definitions = collections.OrderedDict()
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
        if len(list(filter(lambda d: d.limited, self.iter(honor_destroy=True)))) == 0:
            # the run is not limited to anything, so return everything
            return self.iter(honor_destroy=True)
        else:
            return iter(filter(lambda d: d.limited, self.iter(honor_destroy=True)))

    @property
    def body(self):
        return self._body
