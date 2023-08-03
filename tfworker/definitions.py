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

import collections
import copy
import json
from pathlib import Path, PurePath

import click
import hcl2
import jinja2
from mergedeep import merge

from tfworker import constants as const
from tfworker.util.copier import CopyFactory

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
        tf_version_major,
        limited=False,
        template_callback=None,
        use_backend_remotes=False,
    ):
        self.tag = definition
        self._body = body
        self._path = body.get("path")
        self._remote_vars = self.make_vars(
            body.get("remote_vars", dict()), global_remote_vars
        )
        self._terraform_vars = self.make_vars(
            body.get("terraform_vars", dict()), global_terraform_vars
        )
        self._template_vars = self.make_vars(
            body.get("template_vars", dict()), global_template_vars
        )

        self._deployment = deployment
        self._repository_path = repository_path
        self._providers = providers
        self._temp_dir = temp_dir
        self._tf_version_major = tf_version_major
        self._limited = limited

        self._target = f"{self._temp_dir}/definitions/{self.tag}".replace("//", "/")
        self._template_callback = template_callback

        self._use_backend_remotes = use_backend_remotes

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
        """Extract only the providers used by a definition"""
        result = set(self._providers.keys())
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
        """prepare the definitions for running"""

        # prep the definitions
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
        except FileNotFoundError as e:
            if remote_options.get("sub_path", False):
                click.secho(
                    f"could not find sub_path {remote_options['sub_path']} for definition {self.tag}",
                    fg="red",
                )
                raise SystemExit(1)
            else:
                raise e
        except FileExistsError as e:
            raise ReservedFileError(e)
        except RuntimeError as e:
            click.secho(
                f"could not copy source path {self.path} for definition {self.tag}, error details:\n\n{e}",
                fg="red",
            )
            raise SystemExit(1)

        # render the templates
        if self._template_callback is not None:
            self._template_callback(self._target, template_vars=self._template_vars)

        # Create local vars from remote data sources
        if len(list(self._remote_vars.keys())) > 0:
            with open(f"{self._target}/worker-locals.tf", "w+") as tflocals:
                tflocals.write("locals {\n")
                for k, v in self._remote_vars.items():
                    tflocals.write(f"  {k} = data.terraform_remote_state.{v}\n")
                tflocals.write("}\n\n")

        # create remote data sources, and required providers
        if self._use_backend_remotes:
            remotes = backend.remotes()
        else:
            remotes = list(map(lambda x: x.split(".")[0], self._remote_vars.values()))

        with open(f"{self._target}/worker_terraform.tf", "w+") as tffile:
            tffile.write(f"{self._providers.hcl(self.provider_names)}\n\n")
            tffile.write(TERRAFORM_TPL.format(f"{backend.hcl(self.tag)}", ""))
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
        global_vars = global_vars or dict()
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
        elif isinstance(v, dict):
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
        self._definitions = dict()
        self._limit = True if len(limit) > 0 else False
        self._limit_size = len(limit)
        self._root_args = rootc.args

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
                template_callback=self.render_templates,
                use_backend_remotes=self._root_args.backend_use_all_remotes
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

    def render_templates(self, template_path, template_vars={}):
        """render all the .tf.j2 files in a path, and rename them to .tf"""

        def filter_templates(filename):
            """a small function to filter the list of files down to only j2 templates"""
            return filename.endswith(".tf.j2")

        jinja_env = jinja2.Environment(
            undefined=jinja2.StrictUndefined,
            loader=jinja2.FileSystemLoader(template_path),
        )
        jinja_env.globals = merge(
            {},
            self._root_args.template_items(return_as_dict=True, get_env=True),
            {"var": template_vars},
        )

        for template_file in jinja_env.list_templates(filter_func=filter_templates):
            template_target = (
                f"{template_path}/{'.'.join(template_file.split('.')[:-1])}"
            )

            try:
                f = open(template_target, "x")
            except FileExistsError:
                click.secho(
                    f"ERROR: {template_target} already exists! Make sure there's not a .tf and .tf.j2 copy of this file",
                    fg="red",
                )
                raise SystemExit(1)

            try:
                f.writelines(jinja_env.get_template(template_file).generate())
                click.secho(
                    f"rendered {template_file} into {template_target}",
                    fg="yellow",
                )
            except jinja2.exceptions.UndefinedError as e:
                click.secho(
                    f"file contains invalid template substitutions: {e}",
                    fg="red",
                )
                raise SystemExit(1)
            finally:
                f.close()

    @property
    def body(self):
        return self._body
