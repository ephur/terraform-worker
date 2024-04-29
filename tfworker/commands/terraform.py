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

import base64
import json
import os
import pathlib
import re
import shutil

import click

from tfworker.commands.base import BaseCommand
from tfworker.definitions import Definition
from tfworker.handlers.exceptions import HandlerError
from tfworker.util.system import pipe_exec, strip_ansi

TF_STATE_CACHE_NAME = "worker_state_cache.json"


class HookError(Exception):
    pass


class PlanChange(Exception):
    pass


class TerraformError(Exception):
    pass


class TerraformCommand(BaseCommand):
    def __init__(self, rootc, **kwargs):
        super(TerraformCommand, self).__init__(rootc, **kwargs)
        self._destroy = self._resolve_arg("destroy")
        self._tf_apply = self._resolve_arg("tf_apply")
        self._tf_plan = self._resolve_arg("tf_plan")
        self._plan_file_path = self._resolve_arg("plan_file_path")

        if self._tf_apply and self._destroy:
            click.secho("can not apply and destroy at the same time", fg="red")
            raise SystemExit(1)

        if self._backend_plans and not self._plan_file_path:
            # create a plan file path in the tmp dir
            self._plan_file_path = f"{self._temp_dir}/plans"
            pathlib.Path(self._plan_file_path).mkdir(parents=True, exist_ok=True)

        self._b64_encode = self._resolve_arg("b64_encode")
        self._deployment = kwargs["deployment"]
        self._force = self._resolve_arg("force")
        self._show_output = self._resolve_arg("show_output")
        # streaming doesn't allow for distinction between stderr and stdout, but allows
        # terraform operations to be viewed before the process is completed
        self._stream_output = self._resolve_arg("stream_output")
        self._use_colors = True if self._resolve_arg("color") else False
        self._terraform_modules_dir = self._resolve_arg("terraform_modules_dir")
        self._terraform_output = dict()

    @property
    def plan_for(self):
        """plan_for will either be apply or destroy, indicating what action is being planned for"""
        return self._plan_for

    @property
    def tf_version_major(self):
        return self._tf_version_major

    def prep_modules(self):
        """Puts the modules sub directories into place."""

        if self._terraform_modules_dir:
            mod_source = self._terraform_modules_dir
            mod_path = pathlib.Path(mod_source)
            if not mod_path.exists():
                click.secho(
                    f'The specified terraform-modules directory "{mod_source}" does not exists',
                    fg="red",
                )
                raise SystemExit(1)
        else:
            mod_source = f"{self._repository_path}/terraform-modules".replace("//", "/")
            mod_path = pathlib.Path(mod_source)
            if not mod_path.exists():
                click.secho(
                    "The terraform-modules directory does not exist.  Skipping.",
                    fg="green",
                )
                return
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

    def _prep_and_init(self, def_iter: iter = None) -> None:
        """_prep_and_init prepares the modules and runs terraform init"""
        try:
            def_iter = self.definitions.limited()
        except ValueError as e:
            click.secho(f"Error with supplied limit: {e}", fg="red")
            raise SystemExit(1)

        for definition in def_iter:
            # copy definition files / templates etc.
            click.secho(f"preparing definition: {definition.tag}", fg="green")
            definition.prep(self._backend)

            # run terraform init
            try:
                self._run(definition, "init", debug=self._show_output)
            except TerraformError:
                click.secho("error running terraform init", fg="red")
                raise SystemExit(1)

    def _check_plan(self, definition: Definition) -> (bool, bool):
        """determines if a plan is needed"""
        # when no plan path is specified, it's straight forward
        if self._plan_file_path is None:
            if self._tf_plan is False:
                definition._ready_to_apply = True
                return False
            else:
                definition._ready_to_apply = False
                return True

        # a lot more to consider when a plan path is specified
        plan_path = pathlib.Path.absolute(pathlib.Path(self._plan_file_path))
        plan_file = pathlib.Path(
            f"{plan_path}/{self._deployment}_{definition.tag}.tfplan"
        )
        definition.plan_file = plan_file
        click.secho(f"using plan file:{plan_file}", fg="yellow")

        if not (plan_path.exists() and plan_path.is_dir()):
            click.secho(
                f'plan path "{plan_path}" is not suitable, it is not an existing directory'
            )
            raise SystemExit()

        # run all the handlers with with action plan and stage check
        try:
            self._execute_handlers(
                action="plan",
                stage="check",
                deployment=self._deployment,
                definition=definition.tag,
                definition_path=definition.fs_path,
                planfile=plan_file,
            )
        except HandlerError as e:
            if e.terminate:
                click.secho(f"terminating due to fatal handler error {e}", fg="red")
                raise SystemExit(1)
            click.secho(f"handler error: {e}", fg="red")

        # if --no-plan is specified, skip planning step regardless of other conditions
        if self._tf_plan is False:
            definition._ready_to_apply = True
            return False

        # planning was requested, check if existing plan is suitable
        if plan_file.exists():
            if plan_file.stat().st_size == 0:
                click.secho(
                    f"exiting plan file {plan_file} exists but is empty; planning again",
                    fg="green",
                )
                definition._ready_to_apply = False
                return True
            click.secho(
                f"existing plan file {plan_file} is suitable for apply; not planning again; remove plan file to allow planning",
                fg="green",
            )
            definition._ready_to_apply = True
            return False

        # All of the false conditions have been returned, so we need to plan
        definition._ready_to_apply = False
        return True

    def _exec_plan(self, definition) -> bool:
        """_exec_plan executes a terraform plan, returns true if a plan has changes"""
        changes = False

        # call handlers for pre plan
        try:
            self._execute_handlers(
                action="plan",
                stage="pre",
                deployment=self._deployment,
                definition=definition.tag,
                definition_path=definition.fs_path,
            )
        except HandlerError as e:
            if e.terminate:
                click.secho(f"terminating due to fatal handler error {e}", fg="red")
                raise SystemExit(1)
            click.secho(f"handler error: {e}", fg="red")

        click.secho(
            f"planning definition for {self._plan_for}: {definition.tag}",
            fg="green",
        )

        try:
            self._run(
                definition,
                "plan",
                debug=self._show_output,
                plan_action=self._plan_for,
                plan_file=str(definition.plan_file),
            )
        except PlanChange:
            # on destroy, terraform ALWAYS indicates a plan change
            click.secho(f"plan changes for {self._plan_for} {definition.tag}", fg="red")
            definition._ready_to_apply = True
            changes = True
        except TerraformError:
            click.secho(
                f"error planning terraform definition: {definition.tag}!",
                fg="red",
            )
            raise SystemExit(2)

        try:
            self._execute_handlers(
                action="plan",
                stage="post",
                deployment=self._deployment,
                definition=definition.tag,
                definition_path=definition.fs_path,
                text=strip_ansi(self._terraform_output["stdout"].decode()),
                planfile=definition.plan_file,
                changes=changes,
            )
        except HandlerError as e:
            click.secho(f"{e}", fg="red")
            if e.terminate:
                click.secho(f"error is fatal, terminating", fg="red")
                raise SystemExit(1)

        if not changes:
            click.secho(f"no plan changes for {definition.tag}", fg="yellow")

        return changes

    def _check_apply_or_destroy(self, changes, definition) -> bool:
        """_check_apply_or_destroy determines if a terraform execution is needed"""
        # never apply if --no-apply is used
        if self._tf_apply is not True:
            return False

        # if not changes and not force, skip apply
        if not (changes or definition._ready_to_apply) and not self._force:
            click.secho("no changes, skipping terraform apply", fg="yellow")
            return False

        # if the definition plan file exists, and is not empty then apply
        if self._plan_file_path is not None:
            if not definition.plan_file.exists():
                click.secho(
                    f"plan file {definition.plan_file} does not exist, can't apply",
                    fg="red",
                )
                return False

        # if --force is specified, always apply
        if self._force:
            click.secho(
                f"--force specified, proceeding with apply for {definition.tag} anyway",
            )
            return True

        # All of the false conditions have been returned
        return True

    def _exec_apply_or_destroy(self, definition) -> None:
        """_exec_apply_or_destroy executes a terraform apply or destroy"""
        # call handlers for pre apply
        try:
            self._execute_handlers(
                action=self._plan_for,
                stage="pre",
                deployment=self._deployment,
                definition=definition.tag,
                definition_path=definition.fs_path,
                planfile=definition.plan_file,
            )
        except HandlerError as e:
            if e.terminate:
                click.secho(f"terminating due to fatal handler error {e}", fg="red")
                raise SystemExit(1)
            click.secho(f"handler error: {e}", fg="red")

        # execute terraform apply or destroy
        tf_error = False
        try:
            self._run(
                definition,
                self._plan_for,
                debug=self._show_output,
                plan_file=definition.plan_file,
            )
        except TerraformError:
            tf_error = True

        # remove the plan file if it exists
        if definition.plan_file is not None and definition.plan_file.exists():
            definition.plan_file.unlink()

        # call handlers for post apply/destroy
        try:
            self._execute_handlers(
                action=self._plan_for,
                stage="post",
                deployment=self._deployment,
                definition=definition.tag,
                definition_path=definition.fs_path,
                planfile=definition.plan_file,
                error=tf_error,
            )
        except HandlerError as e:
            if e.terminate:
                click.secho(f"terminating due to fatal handler error {e}", fg="red")
                raise SystemExit(1)
            click.secho(f"handler error: {e}", fg="red")

        if tf_error is True:
            click.secho(
                f"error executing terraform {self._plan_for} for {definition.tag}",
                fg="red",
            )
            raise SystemExit(2)
        else:
            click.secho(
                f"terraform {self._plan_for} complete for {definition.tag}",
                fg="green",
            )

    def exec(self):
        """exec handles running the terraform chain"""
        try:
            def_iter = self.definitions.limited()
        except ValueError as e:
            click.secho(f"Error with supplied limit: {e}", fg="red")
            raise SystemExit(1)

        # prepare the modules and run terraform init
        self._prep_and_init(def_iter)

        for definition in def_iter:
            changes = None
            # exec planning step if we should
            if self._check_plan(definition):
                changes = self._exec_plan(definition)
            # exec apply step if we should
            if self._check_apply_or_destroy(changes, definition):
                self._exec_apply_or_destroy(definition)

    def _run(
        self, definition, command, debug=False, plan_action="init", plan_file=None
    ):
        """Run terraform."""

        color_str = "-no-color" if self._use_colors is False else ""
        params = {
            "init": f"-input=false {color_str} -plugin-dir={self._temp_dir}/terraform-plugins",
            "plan": f"-input=false -detailed-exitcode {color_str}",
            "apply": f"-input=false {color_str} -auto-approve",
            "destroy": f"-input=false {color_str} -auto-approve",
        }

        if plan_action == "destroy":
            params["plan"] += " -destroy"

        if plan_file is not None:
            params["plan"] += f" -out {plan_file}"
            params["apply"] += f" {plan_file}"

        env = os.environ.copy()

        # acknowledge that we are using a plugin cache; and compute the lockfile each run
        env["TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE"] = "1"
        # reduce non essential terraform output
        env["TF_IN_AUTOMATION"] = "1"

        for auth in self._authenticators:
            env.update(auth.env())

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
                    extra_vars=definition.template_vars,
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
        (exit_code, stdout, stderr) = pipe_exec(
            f"{self._terraform_bin} {command} {command_params}",
            cwd=working_dir,
            env=env,
            stream_output=self._stream_output,
        )
        click.secho(f"exit code: {exit_code}", fg="blue")
        (
            self._terraform_output["exit_code"],
            self._terraform_output["stdout"],
            self._terraform_output["stderr"],
        ) = (exit_code, stdout, stderr)

        if debug and not self._stream_output:
            for line in stdout.decode().splitlines():
                click.secho(f"stdout: {line}", fg="blue")
            for line in stderr.decode().splitlines():
                click.secho(f"stderr: {line}", fg="red")

        # If a plan file was saved, write the plan output
        if plan_file is not None:
            plan_log = f"{os.path.splitext(plan_file)[0]}.log"

            with open(plan_log, "w") as pl:
                pl.write("STDOUT:\n")
                for line in stdout.decode().splitlines():
                    pl.write(f"{line}\n")
                pl.write("\nSTDERR:\n")
                for line in stderr.decode().splitlines():
                    pl.write(f"{line}\n")

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
                    extra_vars=definition.template_vars,
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
        extra_vars={},
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
        local_env["TF_PATH"] = terraform_path
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
                            state_value = base64.b64encode(state_value.encode("utf-8"))
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
                        tf_var[1] = base64.b64encode(tf_var[1].encode("utf-8"))

                    local_env[f"TF_VAR_{tf_var[0].upper()}"] = tf_var[1]
        else:
            click.secho(
                f"{working_dir}/worker.auto.tfvars not found!",
                fg="red",
            )

        for k, v in extra_vars.items():
            if b64_encode:
                v = base64.b64encode(v.encode("utf-8"))
            local_env[f"TF_EXTRA_{k.upper()}"] = v

        # execute the hook
        (exit_code, stdout, stderr) = pipe_exec(
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
        The general handler function for getting a state item, it will first
        try to get the item from another definitions output, but if the other
        definition is not setup, it will fallback to getting the item from the
        remote state.

        @param working_dir: The working directory of the terraform definition
        @param env: The environment variables to pass to the terraform command
        @param terraform_bin: The path to the terraform binary
        @param state: The state name to get the item from
        @param item: The item to get from the state
        """
        try:
            return TerraformCommand._get_state_item_from_output(
                working_dir, env, terraform_bin, state, item
            )
        except FileNotFoundError:
            return TerraformCommand._get_state_item_from_remote(
                working_dir, env, terraform_bin, state, item
            )

    @staticmethod
    def _get_state_item_from_remote(working_dir, env, terraform_bin, state, item):
        """
        get_state_item returns json encoded output from a terraform remote state

        @param working_dir: The working directory of the terraform definition
        @param env: The environment variables to pass to the terraform command
        @param terraform_bin: The path to the terraform binary
        @param state: The state name to get the item from
        @param item: The item to get from the state
        """

        remote_state = None

        # setup the state cache
        cache_file = TerraformCommand._get_cache_name(working_dir)
        TerraformCommand._make_state_cache(working_dir, env, terraform_bin)

        # read the cache
        with open(cache_file, "r") as f:
            state_cache = json.load(f)

        # Get the remote state we are looking for, and raise an error if it's not there
        resources = state_cache["values"]["root_module"]["resources"]
        for resource in resources:
            if (
                resource["type"] == "terraform_remote_state"
                and resource["name"] == state
            ):
                remote_state = resource
        if remote_state is None:
            raise HookError(f"Remote state item {state} not found")

        if item in remote_state["values"]["outputs"]:
            return json.dumps(
                remote_state["values"]["outputs"][item],
                indent=None,
                separators=(",", ":"),
            )

        raise HookError(f"Remote state item {state}.{item} not found in state cache")

    @staticmethod
    def _get_state_item_from_output(working_dir, env, terraform_bin, state, item):
        """
        Get a single item from the terraform output, this is the preferred
        mechanism as items will be more guaranteed to be up to date, but it
        creates problems when the remote state is not setup, like when using
        --limit

        @param work_dir: The working directory of the terraform definition
        @param env: The environment variables to pass to the terraform command
        @param terraform_bin: The path to the terraform binary
        @param state: The state name to get the item from
        @param item: The item to get from the state
        """

        base_dir, _ = os.path.split(working_dir)
        try:
            (exit_code, stdout, stderr) = pipe_exec(
                f"{terraform_bin} output -json -no-color {item}",
                cwd=f"{base_dir}/{state}",
                env=env,
            )
        except FileNotFoundError:
            # the remote state is not setup, likely do to use of --limit
            # this is acceptable, and is the responsibility of the hook
            # to ensure it has all values needed for safe execution
            raise

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
    def _make_state_cache(
        working_dir: str, env: dict, terraform_bin: str, refresh: bool = False
    ):
        """
        Using `terraform show -json` make a cache of the state file

        @param working_dir: The working directory of the terraform definition
        @param env: The environment variables to pass to the terraform command
        @param terraform_bin: The path to the terraform binary
        @param refresh: If true, the cache will be refreshed
        """

        # check if the cache exists
        state_cache = TerraformCommand._get_cache_name(working_dir)
        if not refresh and os.path.exists(state_cache):
            return

        # ensure the state is refreshed; but no changes to resources are made
        (exit_code, stdout, stderr) = pipe_exec(
            f"{terraform_bin} apply -auto-approve -refresh-only",
            cwd=working_dir,
            env=env,
        )

        # get the json from terraform to generate the cache
        (exit_code, stdout, stderr) = pipe_exec(
            f"{terraform_bin} show -json",
            cwd=working_dir,
            env=env,
        )

        # validate the output, check exit code and ensure output is json
        if exit_code != 0:
            raise HookError(f"Error reading terraform state, details: {stderr}")
        try:
            json.loads(stdout)
        except json.JSONDecodeError:
            raise HookError(
                f"Error parsing terraform state; output is not in json format"
            )

        # write the cache to disk
        try:
            with open(state_cache, "w") as f:
                f.write(stdout.decode())
        except Exception as e:
            raise HookError(f"Error writing state cache to {state_cache}, details: {e}")
        return

    @staticmethod
    def _get_cache_name(working_dir: str) -> str:
        """
        Get the cache directory for the state cache

        @param working_dir: The working directory of the terraform definition

        @return: The cache directory path
        """
        return f"{working_dir}/{TF_STATE_CACHE_NAME}"
