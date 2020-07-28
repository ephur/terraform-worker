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

import os
import glob
import shutil
import sys
import urllib
import zipfile
import copy

from pathlib import Path

import click
import jinja2

from .main import pipe_exec


class TerraformError(Exception):
    pass


class PlanChange(Exception):
    pass


def download_plugins(plugins, temp_dir):
    """
    Download the required plugins.

    This could be further optimized to not download plugins from hashicorp, but rather have them in a local repository
    or host them in s3, and get them from an internal s3 endpoint so no transit charges are incurred. Ideally these
    would be stored between runs, and only downloaded if the versions have changed. In production try  to remove all
    all external repositories/sources from the critical path.
    """
    platform = "{}_{}".format(sys.platform.rstrip("2"), "amd64")
    plugin_dir = "{}/terraform-plugins".format(temp_dir)
    if not os.path.isdir(plugin_dir):
        os.mkdir(plugin_dir)

    for name, details in plugins.items():
        # Get platform and strip 2 off linux 2.x kernels
        file_name = "terraform-provider-{}_{}_{}.zip".format(
            name, details["version"], platform
        )
        uri = "https://releases.hashicorp.com/terraform-provider-{}/{}/{}".format(
            name, details["version"], file_name
        )
        click.secho(
            "getting plugin: {} version {} from {}".format(
                name, details["version"], uri
            ),
            fg="yellow",
        )

        with urllib.request.urlopen(uri) as response, open(
            "{}/{}".format(plugin_dir, file_name), "wb"
        ) as plug_file:
            shutil.copyfileobj(response, plug_file)
        with zipfile.ZipFile("{}/{}".format(plugin_dir, file_name)) as zip_file:
            zip_file.extractall(plugin_dir)
        os.remove("{}/{}".format(plugin_dir, file_name))

        files = glob.glob("{}/terraform-provider*".format(plugin_dir))
        for afile in files:
            os.chmod(afile, 0o755)


def prep_modules(src, dst):
    """Puts the modules sub directories into place."""
    mod_source = "{}/terraform-modules".format(src).replace("//", "/")
    mod_destination = "{}/terraform-modules".format(dst).replace("//", "/")
    shutil.copytree(
        mod_source,
        mod_destination,
        symlinks=True,
        ignore=shutil.ignore_patterns("test", ".terraform", "terraform.tfstate*"),
    )


def prep_def(name, definition, all_defs, temp_dir, repo_path, deployment, args):
    """ prepare the definitions for running """
    repo = Path("{}/{}".format(repo_path, definition["path"]).replace("//", "/"))
    target = Path("{}/definitions/test/{}".format(temp_dir, name).replace("//", "/"))
    target.mkdir(parents=True, exist_ok=True)

    if not repo.exists():
        click.secho(
            "Error preparing definition {}, path {} does not exist".format(
                name, repo.resolve()
            ),
            fg="red",
        )
        sys.exit(1)

    # Prepare variables
    terraform_vars = None
    template_vars = make_vars("template_vars", definition, all_defs)
    terraform_vars = make_vars("terraform_vars", definition, all_defs)
    locals_vars = make_vars("remote_vars", definition)
    template_vars["deployment"] = deployment
    terraform_vars["deployment"] = deployment

    # Put terraform files in place
    for tf in repo.glob("*.tf"):
        shutil.copy("{}".format(str(tf)), str(target))
    for tf in repo.glob("*.tfvars"):
        shutil.copy("{}".format(str(tf)), str(target))
    if os.path.isdir(str(repo) + "/templates".replace("//", "/")):
        shutil.copytree(
            "{}/templates".format(str(repo)), "{}/templates".format(str(target))
        )
    if os.path.isdir(str(repo) + "/policies".replace("//", "/")):
        shutil.copytree(
            "{}/policies".format(str(repo)), "{}/policies".format(str(target))
        )
    if os.path.isdir(str(repo) + "/scripts".replace("//", "/")):
        shutil.copytree(
            "{}/scripts".format(str(repo)), "{}/scripts".format(str(target))
        )
    if os.path.isdir(str(repo) + "/repos".replace("//", "/")):
        shutil.copytree("{}/repos".format(str(repo)), "{}/repos".format(str(target)))

    # Render jinja templates and put in place
    env = jinja2.Environment(loader=jinja2.FileSystemLoader)

    for j2 in repo.glob("*.j2"):
        contents = env.get_template(str(j2)).render(**template_vars)
        with open("{}/{}".format(str(target), str(j2)), "w+") as j2_file:
            j2_file.write(contents)

    # Create local vars from remote data sources
    if len(list(locals_vars.keys())) > 0:
        with open("{}/{}".format(str(target), "worker-locals.tf"), "w+") as tflocals:
            tflocals.write("locals {\n")
            for k, v in locals_vars.items():
                tflocals.write(
                    '  {} = "${{data.terraform_remote_state.{}}}"\n'.format(k, v)
                )
            tflocals.write("}\n\n")

    # Create the terraform configuration, terraform.tf
    state = render_remote_state(name, deployment, args)
    remote_data = render_remote_data_sources(all_defs["definitions"], name, args)
    providers = render_providers(all_defs["providers"], args)
    with open("{}/{}".format(str(target), "terraform.tf"), "w+") as tffile:
        tffile.write("{}\n\n".format(providers))
        tffile.write("{}\n\n".format(state))
        tffile.write("{}\n\n".format(remote_data))

    # Create the variable definitions
    with open("{}/{}".format(str(target), "worker.auto.tfvars"), "w+") as varfile:
        for k, v in terraform_vars.items():
            if isinstance(v, list):
                varstring = "[{}]".format(", ".join(map(quote_str, v)))
                varfile.write("{} = {}\n".format(k, varstring))
            else:
                varfile.write('{} = "{}"\n'.format(k, v))


def make_vars(section, single, base=None):
    """Make a variables dictionary based on default vars, as well as specific vars for an item."""
    if base is None:
        base = {}

    item_vars = copy.deepcopy(base.get(section, {}))
    for k, v in single.get(section, {}).items():
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


def quote_str(string):
    """Put literal quotes around a string."""
    return '"{}"'.format(string)


def render_remote_state(name, deployment, args):
    """Return remote for the definition."""
    state_config = []
    state_config.append("terraform {")
    state_config.append('  backend "s3" {')
    state_config.append('    region = "{}"'.format(args.state_region))
    state_config.append('    bucket = "{}"'.format(args.s3_bucket))
    state_config.append(
        '    key = "{}/{}/terraform.tfstate"'.format(args.s3_prefix, name)
    )
    state_config.append('    dynamodb_table = "terraform-{}"'.format(deployment))
    state_config.append('    encrypt = "true"')
    state_config.append("  }")
    state_config.append("}")
    return "\n".join(state_config)


def render_remote_data_sources(definitions, exclude, args):
    """Return remote data sources for all items until the excluded item"""
    remote_data_config = []
    for name, _ in definitions.items():
        if name == exclude:
            break
        remote_data_config.append('data "terraform_remote_state" "{}" {{'.format(name))
        remote_data_config.append('  backend = "s3"')
        remote_data_config.append("  config = {")
        remote_data_config.append('    region = "{}"'.format(args.state_region))
        remote_data_config.append('    bucket = "{}"'.format(args.s3_bucket))
        remote_data_config.append(
            '    key = "{}/{}/terraform.tfstate"'.format(args.s3_prefix, name)
        )
        remote_data_config.append("  }")
        remote_data_config.append("}\n")
    return "\n".join(remote_data_config)


def render_providers(providers, args):
    """Return a string that provides the provider configuration."""
    # prov_string as a list is funny sounding, but it gets joined and returned as a string
    prov_string = []
    for provider in providers:
        provider_vars = {}
        try:
            for k, v in providers[provider]["vars"].items():
                provider_vars[k] = v
        except (KeyError, TypeError):
            """No provider vars were set."""
            pass
        prov_string.append('provider "{}" {{'.format(provider))
        for k, v in provider_vars.items():
            prov_string.append('  {} = "{}"'.format(k, v))
        prov_string.append("}")
    return "\n".join(prov_string)


def run(
    name,
    temp_dir,
    terrform_path,
    command,
    key_id,
    key_secret,
    key_token=None,
    debug=False,
    plan_action="apply",
):
    """Run terraform."""
    params = {
        "init": "-input=false -no-color",
        "plan": "-input=false -detailed-exitcode -no-color",
        "apply": "-input=false -no-color -auto-approve",
        "destroy": "-input=false -no-color -force",
    }

    if plan_action == "destroy":
        params["plan"] += " -destroy"

    env = os.environ.copy()
    env["AWS_ACCESS_KEY_ID"] = key_id
    env["AWS_SECRET_ACCESS_KEY"] = key_secret
    if key_token is not None:
        env["AWS_SESSION_TOKEN"] = key_token
    env["TF_PLUGIN_CACHE_DIR"] = "{}/terraform-plugins".format(temp_dir)

    working_dir = "{}/definitions/test/{}".format(temp_dir, name)
    command_params = params.get(command)
    if not command_params:
        raise ValueError(
            "invalid command passed to terraform, {} has no defined params!".format(
                command
            )
        )

    (exit_code, stdout, stderr) = pipe_exec(
        "{} {} {}".format(terrform_path, command, command_params),
        cwd=working_dir,
        env=env,
    )
    if debug:
        click.secho("exit code: {}".format(exit_code), fg="blue")
        for line in stdout.decode().splitlines():
            click.secho("stdout: {}".format(line), fg="blue")
        for line in stderr.decode().splitlines():
            click.secho("stderr: {}".format(line), fg="red")

    # special handling of the exit codes for "plan" operations
    if command == "plan":
        if exit_code == 0:
            return True
        if exit_code == 1:
            raise TerraformError
        if exit_code == 2:
            raise PlanChange

    if exit_code:
        raise TerraformError

    return True
