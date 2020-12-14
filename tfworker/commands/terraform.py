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

import base64
import json
import os
import re
import shlex
import shutil
import subprocess

import click
from tfworker.commands.base import BaseCommand


class HookError(Exception):
    pass


class PlanChange(Exception):
    pass


class TerraformError(Exception):
    pass


class TerraformCommand(BaseCommand):
    def __init__(self, rootc, **kwargs):
        self._destroy = kwargs.get("destroy")
        self._tf_apply = kwargs.get("tf_apply")
        if self._tf_apply and self._destroy:
            click.secho("can not apply and destroy at the same time", fg="red")
            raise SystemExit(1)

        self._b64_encode = kwargs.get("b64_encode")
        self._deployment = kwargs.get("deployment")
        self._force_apply = kwargs.get("force_apply")
        self._show_output = kwargs.get("show_output")
        self._terraform_bin = kwargs.get("terraform_bin")

        self._plan_for = "destroy" if kwargs.get("destroy") else "apply"
        (self._tf_version_major, self._tf_version_minor) = kwargs.get(
            "tf_version", (None, None)
        )
        if self._tf_version_major is None or self._tf_version_minor is None:
            (
                self._tf_version_major,
                self._tf_version_minor,
            ) = self.get_terraform_version(self._terraform_bin)
        super(TerraformCommand, self).__init__(rootc, plan_for=self._plan_for, **kwargs)

    @property
    def plan_for(self):
        return self._plan_for

    def prep_modules(self):
        """Puts the modules sub directories into place."""
        mod_source = f"{self._repository_path}/terraform-modules".replace("//", "/")
        mod_destination = f"{self._temp_dir}/terraform-modules".replace("//", "/")
        click.secho(
            f"copying modules from {mod_source} to {mod_destination}", fg="yellow"
        )
        shutil.copytree(
            mod_source,
            mod_destination,
            symlinks=True,
            ignore=shutil.ignore_patterns("test", ".terraform", "terraform.tfstate*"),
        )

    def exec(self):
        for definition in self.definitions.limited():
            execute = False
            # copy definition files / templates etc.
            click.secho(f"preparing definition: {definition.tag}", fg="green")
            definition.prep(
                self._backend,
            )
            # run terraform init
            try:
                self._run(definition, "init", debug=self._show_output)
            except TerraformError:
                click.secho("error running terraform init", fg="red")
                raise SystemExit(1)

            click.secho(f"planning definition: {definition.tag}", fg="green")

            # run terraform plan
            try:
                self._run(
                    definition,
                    "plan",
                    debug=self._show_output,
                    plan_action=self._plan_for,
                )
            except PlanChange:
                execute = True
            except TerraformError:
                click.secho(
                    f"error planning terraform definition: {definition.tag}!",
                    fg="red",
                )
                raise SystemExit(2)

            if self._force_apply:
                execute = True

            if execute and self._tf_apply:
                if self._force_apply:
                    click.secho(
                        f"force apply for {definition.tag}, applying",
                        fg="yellow",
                    )
                else:
                    click.secho(
                        f"plan changes for {definition.tag}, applying",
                        fg="yellow",
                    )
            elif execute and self._destroy:
                click.secho(
                    f"plan changes for {definition.tag}, destroying",
                    fg="yellow",
                )
            elif not execute:
                click.secho(f"no plan changes for {definition.tag}", fg="yellow")
                continue

            try:
                self._run(
                    definition,
                    self._plan_for,
                    debug=self._show_output,
                )
            except TerraformError:
                click.secho(
                    f"error with terraform {self._plan_for} on definition"
                    f" {definition.tag}, exiting",
                    fg="red",
                )
                raise SystemExit(2)
            else:
                click.secho(
                    f"terraform {self._plan_for} complete for {definition.tag}",
                    fg="green",
                )

    def _run(self, definition, command, debug=False, plan_action="init"):
        """Run terraform."""
        if self._tf_version_major == 12:
            params = {
                "init": f"-input=false -no-color -plugin-dir={self._temp_dir}/terraform-plugins",
                "plan": "-input=false -detailed-exitcode -no-color",
                "apply": "-input=false -no-color -auto-approve",
                "destroy": "-input=false -no-color -force",
            }
        else:
            params = {
                "init": "-input=false -no-color",
                "plan": "-input=false -detailed-exitcode -no-color",
                "apply": "-input=false -no-color -auto-approve",
                "destroy": "-input=false -no-color -force",
            }

        if plan_action == "destroy":
            params["plan"] += " -destroy"

        env = os.environ.copy()
        for auth in self._authenticators:
            env.update(auth.env())

        env["TF_PLUGIN_CACHE_DIR"] = f"{self._temp_dir}/terraform-plugins"

        working_dir = f"{self._temp_dir}/definitions/{definition.tag}"
        command_params = params.get(command)
        if not command_params:
            raise ValueError(
                f"invalid command passed to terraform, {command} has no defined params!"
            )

        # only execute hooks for plan/apply/destroy
        try:
            if TerraformCommand.check_hooks(
                "pre", working_dir, command
            ) and command in ["apply", "destroy", "plan"]:
                # pre exec hooks
                # want to pass remotes
                # want to pass tf_vars
                click.secho(
                    f"found pre-{command} hook script for definition {definition.tag},"
                    " executing ",
                    fg="yellow",
                )
                TerraformCommand.hook_exec(
                    "pre",
                    command,
                    working_dir,
                    env,
                    self._terraform_bin,
                    debug=debug,
                    b64_encode=self._b64_encode,
                )
        except HookError as e:
            click.secho(
                f"hook execution error on definition {definition.tag}: {e}",
                fg="red",
            )
            raise SystemExit(2)

        click.secho(
            f"cmd: {self._terraform_bin} {command} {command_params}", fg="yellow"
        )
        (exit_code, stdout, stderr) = TerraformCommand.pipe_exec(
            f"{self._terraform_bin} {command} {command_params}",
            cwd=working_dir,
            env=env,
        )
        if debug:
            click.secho(f"exit code: {exit_code}", fg="blue")
            for line in stdout.decode().splitlines():
                click.secho(f"stdout: {line}", fg="blue")
            for line in stderr.decode().splitlines():
                click.secho(f"stderr: {line}", fg="red")

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

        # only execute hooks for plan/destroy
        try:
            if TerraformCommand.check_hooks(
                "post", working_dir, command
            ) and command in ["apply", "destroy", "plan"]:
                click.secho(
                    f"found post-{command} hook script for definition {definition.tag},"
                    " executing ",
                    fg="yellow",
                )
                TerraformCommand.hook_exec(
                    "post",
                    command,
                    working_dir,
                    env,
                    self._terraform_bin,
                    debug=debug,
                    b64_encode=self._b64_encode,
                )
        except HookError as e:
            click.secho(
                f"hook execution error on definition {definition.tag}: {e}", fg="red"
            )
            raise SystemExit(2)
        return True

    @staticmethod
    def hook_exec(
        phase,
        command,
        working_dir,
        env,
        terraform_path,
        debug=False,
        b64_encode=False,
    ):
        """
        hook_exec executes a hook script.

        Before execution it sets up the environment to make all terraform and remote
        state variables available to the hook via environment vars
        """

        key_replace_items = {
            " ": "",
            '"': "",
            "-": "_",
            ".": "_",
        }
        val_replace_items = {
            " ": "",
            '"': "",
            "\n": "",
        }
        local_env = env.copy()
        hook_dir = f"{working_dir}/hooks"
        hook_script = None

        for f in os.listdir(hook_dir):
            # this file format is specifically structured by the prep_def function
            if os.path.splitext(f)[0] == f"{phase}_{command}":
                hook_script = f"{hook_dir}/{f}"
        # this should never have been called if the hook script didn't exist...
        if hook_script is None:
            raise HookError(f"hook script missing from {hook_dir}")

        # populate environment with terraform remotes
        if os.path.isfile(f"{working_dir}/worker-locals.tf"):
            # I'm sorry. :-)
            r = re.compile(
                r"\s*(?P<item>\w+)\s*\=.+data\.terraform_remote_state\.(?P<state>\w+)\.outputs\.(?P<state_item>\w+)\s*"
            )

            with open(f"{working_dir}/worker-locals.tf") as f:
                for line in f:
                    m = r.match(line)
                    if m:
                        item = m.group("item")
                        state = m.group("state")
                        state_item = m.group("state_item")
                    else:
                        continue

                    state_value = TerraformCommand.get_state_item(
                        working_dir, env, terraform_path, state, state_item
                    )

                    if state_value is not None:
                        if b64_encode:
                            state_value = base64.b64encode(
                                state_value.encode("utf-8")
                            ).decode()
                        local_env[f"TF_REMOTE_{state}_{item}".upper()] = state_value

        # populate environment with terraform variables
        if os.path.isfile(f"{working_dir}/worker.auto.tfvars"):
            with open(f"{working_dir}/worker.auto.tfvars") as f:
                for line in f:
                    tf_var = line.split("=")

                    # strip bad names out for env var settings
                    for k, v in key_replace_items.items():
                        tf_var[0] = tf_var[0].replace(k, v)

                    for k, v in val_replace_items.items():
                        tf_var[1] = tf_var[1].replace(k, v)

                    if b64_encode:
                        tf_var[1] = base64.b64encode(tf_var[1].encode("utf-8")).decode()

                    local_env[f"TF_VAR_{tf_var[0].upper()}"] = tf_var[1]
        else:
            click.secho(
                f"{working_dir}/worker.auto.tfvars not found!",
                fg="red",
            )

        # execute the hook
        (exit_code, stdout, stderr) = TerraformCommand.pipe_exec(
            f"{hook_script} {phase} {command}",
            cwd=hook_dir,
            env=local_env,
        )

        # handle output from hook_script
        if debug:
            click.secho(f"exit code: {exit_code}", fg="blue")
            for line in stdout.decode().splitlines():
                click.secho(f"stdout: {line}", fg="blue")
            for line in stderr.decode().splitlines():
                click.secho(f"stderr: {line}", fg="red")

        if exit_code != 0:
            raise HookError("hook script {}")

    @staticmethod
    def pipe_exec(args, stdin=None, cwd=None, env=None):
        """
        A function to accept a list of commands and pipe them together.

        Takes optional stdin to give to the first item in the pipe chain.
        """
        count = 0
        commands = []
        if env is None:
            env = os.environ.copy()

        if not isinstance(args, list):
            args = [args]

        for i in args:
            if count == 0:
                if stdin is None:
                    commands.append(
                        subprocess.Popen(
                            shlex.split(i),
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            cwd=cwd,
                            env=env,
                        )
                    )
                else:
                    commands.append(
                        subprocess.Popen(
                            shlex.split(i),
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            stdin=subprocess.PIPE,
                            cwd=cwd,
                            env=env,
                        )
                    )
            else:
                commands.append(
                    subprocess.Popen(
                        shlex.split(i),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        stdin=commands[count - 1].stdout,
                        cwd=cwd,
                        env=env,
                    )
                )
            count = count + 1

        if stdin is not None:
            stdin_bytes = stdin.encode()
            if len(commands) > 1:
                commands[0].communicate(input=stdin_bytes)
                stdout, stderr = commands[-1].communicate()
                commands[-1].wait()
                returncode = commands[-1].returncode
            else:
                stdout, stderr = commands[0].communicate(input=stdin_bytes)
                commands[0].wait()
                returncode = commands[0].returncode
        else:
            stdout, stderr = commands[-1].communicate()
            commands[-1].wait()
            returncode = commands[-1].returncode

        return (returncode, stdout, stderr)

    @staticmethod
    def check_hooks(phase, working_dir, command):
        """
        check_hooks determines if a hook exists for a given operation/definition
        """
        hook_dir = f"{working_dir}/hooks"
        if not os.path.isdir(hook_dir):
            # there is no hooks dir
            return False
        for f in os.listdir(hook_dir):
            if os.path.splitext(f)[0] == f"{phase}_{command}":
                if os.access(f"{hook_dir}/{f}", os.X_OK):
                    return True
                else:
                    raise HookError(f"{hook_dir}/{f} exists, but is not executable!")
        return False

    @staticmethod
    def get_state_item(working_dir, env, terraform_bin, state, item):
        """
        get_state_item returns json encoded output from a terraform remote state
        """
        base_dir, _ = os.path.split(working_dir)
        try:
            (exit_code, stdout, stderr) = TerraformCommand.pipe_exec(
                f"{terraform_bin} output -json -no-color {item}",
                cwd=f"{base_dir}/{state}",
                env=env,
            )
        except FileNotFoundError:
            # the remote state is not setup, likely do to use of --limit
            # this is acceptable, and is the responsibility of the hook
            # to ensure it has all values needed for safe execution
            return None

        if exit_code != 0:
            raise HookError(
                f"Error reading remote state item {state}.{item}, details: {stderr}"
            )

        if stdout is None:
            raise HookError(
                f"Remote state item {state}.{item} is empty; This is completely"
                " unexpected, failing..."
            )
        json_output = json.loads(stdout)
        return json.dumps(json_output, indent=None, separators=(",", ":"))

    @staticmethod
    def get_terraform_version(terraform_bin):
        (return_code, stdout, stderr) = TerraformCommand.pipe_exec(
            f"{terraform_bin} version"
        )
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
