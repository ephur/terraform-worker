import os
import pathlib
from typing import TYPE_CHECKING

import click

import tfworker.util.hooks as hooks
import tfworker.util.log as log
import tfworker.util.terraform as tf_util
from tfworker.commands.base import BaseCommand
from tfworker.definitions import Definition
from tfworker.exceptions import (
    HandlerError,
    HookError,
    PlanChange,
    TerraformError,
    TFWorkerException,
)
from tfworker.util.system import pipe_exec, strip_ansi

if TYPE_CHECKING:
    from tfworker.app_state import AppState


class TerraformCommand(BaseCommand):
    """
    The TerraformCommand class is called by the top level CLI
    as part of the `terraform` sub-command. It inherits from
    BaseCommand which sets up the application state.

    This class may contain various methods that are used to
    orchestrate the terraform workflow. The methods in this
    class should be limited to providing error handling and
    orchestration of the terraform workflow.

    If you are tempted to override the `__init__` method,
    reconsider the strategy for what you're about to add
    """

    ##################
    # Public methods #
    ##################
    def prep_providers(self) -> None:
        """
        Prepare / Mirror the providers
        """
        if self.app_state.terraform_options.provider_cache is None:
            log.trace("no provider cache specified, skipping provider mirroring")
            return

        log.trace(
            f"using provider cache path: {self.app_state.terraform_options.provider_cache}"
        )
        try:
            tf_util.mirror_providers(
                self.app_state.providers,
                self.app_state.terraform_options.terraform_bin,
                self.app_state.root_options.working_dir,
                self.app_state.terraform_options.provider_cache,
            )
        except TFWorkerException as e:
            log.error(f"error mirroring providers: {e}")
            self.ctx.exit(1)

    # def terraform_init(self) -> None:
    #     """
    #     Handle execution of terraform init for all of the definitions
    #     """
    #     # generate an iterator for the specified definitions (or all if no limit is specified)
    #     ctx, app_state = self._get_state()

    #     # prepare the definitions and run terraform init
    #     log.trace("preparing definitions and running terraform init")
    #     self._prep_and_init(list(app_state.definitions))

    #     # for definition in def_iter:
    #     #     # Execute plan if needed
    #     #     changes = (
    #     #         self._exec_plan(definition) if self._check_plan(definition) else None
    #     #     )

    #     #     # execute apply or destroy if needed
    #     #     if self._check_apply_or_destroy(changes, definition):
    #     #         self._exec_apply_or_destroy(definition)

    ###################
    # Private methods #
    ###################

    ###########################################
    # Methods for dealing with terraform init #
    ###########################################
    def terraform_init(self) -> None:
        from tfworker.definitions.prepare import TerraformPrepare

        tfp = TerraformPrepare(self.ctx, self.app_state)

        for name in self.app_state.definitions.keys():
            log.info(f"preparing definition: {name}")
            tfp.copy_files(name=name)
            try:
                tfp.render_templates(name=name)
                tfp.create_local_vars(name=name)
                tfp.create_terraform_vars(name=name)
                tfp.create_worker_tf(name=name)
                tfp.create_terraform_lockfile(name=name)
            except TFWorkerException as e:
                log.error(f"error rendering templates for definition {name}: {e}")
                self.ctx.exit(1)

            try:
                self._run(name, "init")
            except TerraformError:
                click.secho("error running terraform init", fg="red")
                raise SystemExit(1)

    ###########################################
    # Methods for dealing with terraform plan #
    ###########################################
    def _check_plan(self, definition: Definition) -> bool:
        """
        Determines if a plan is needed for the provided definition

        Args:
            definition: the definition to check for a plan

        Returns:
            bool: True if a plan is needed, False otherwise
        """
        if not self._plan_file_path:
            return self._handle_no_plan_path(definition)

        plan_file = self._prepare_plan_file(definition)
        self._validate_plan_path(plan_file.parent)
        self._run_handlers(definition, "plan", "check", plan_file=plan_file)

        return self._should_plan(definition, plan_file)

    def _handle_no_plan_path(self, definition: Definition) -> bool:
        """Handles the case where no plan path is specified, saved plans are not possible

        Args:
            definition: the definition to check for a plan

        Returns:
            bool: True if a plan is needed, False otherwise
        """

        if not self._tf_plan:
            definition._ready_to_apply = True
            return False
        definition._ready_to_apply = False
        return True

    def _prepare_plan_file(self, definition: Definition) -> pathlib.Path:
        """Prepares the plan file for the definition

        Args:
            definition: the definition to prepare the plan file for

        Returns:
            pathlib.Path: the path to the plan file
        """
        plan_path = pathlib.Path(self._plan_file_path).resolve()
        plan_file = plan_path / f"{self._deployment}_{definition.tag}.tfplan"
        definition.plan_file = plan_file
        click.secho(f"using plan file:{plan_file}", fg="yellow")
        return plan_file

    def _validate_plan_path(self, plan_path: pathlib.Path) -> None:
        """Validates the plan path

        Args:
            plan_path: the path to the plan file

        Returns:
            None
        """
        if not (plan_path.exists() and plan_path.is_dir()):
            click.secho(
                f'plan path "{plan_path}" is not suitable, it is not an existing directory'
            )
            raise SystemExit(1)

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
                click.secho("error is fatal, terminating", fg="red")
                raise SystemExit(1)

        if not changes:
            click.secho(f"no plan changes for {definition.tag}", fg="yellow")

        return changes

    def _should_plan(self, definition: Definition, plan_file: pathlib.Path) -> bool:
        if not self._tf_plan:
            definition._ready_to_apply = True
            return False

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

        definition._ready_to_apply = False
        return True

    ####################################################
    # Methods for dealing with terraform apply/destroy #
    ####################################################
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

    #####################################
    # Methods for dealing with handlers #
    #####################################
    def _run_handlers(
        self, definition, action, stage, plan_file=None, **kwargs
    ) -> None:
        """Runs the handlers for the given action and stage

        Args:
            definition: the definition to run the handlers for
            action: the action to run the handlers for
            stage: the stage to run the handlers for
            plan_file: the plan file to pass to the handlers
            kwargs: additional keyword arguments to pass to the handlers

        Returns:
            None
        """
        try:
            self._execute_handlers(
                action=action,
                stage=stage,
                deployment=self._deployment,
                definition=definition.tag,
                definition_path=definition.fs_path,
                planfile=plan_file,
                **kwargs,
            )
        except HandlerError as e:
            if e.terminate:
                click.secho(f"terminating due to fatal handler error {e}", fg="red")
                raise SystemExit(1)
            click.secho(f"handler error: {e}", fg="red")

    ########################################
    # Common methods for running terraform #
    ########################################
    def _run(
        self,
        definition_name: str,
        command,
        debug=False,
        plan_action="init",
        plan_file=None,
    ):
        """Run terraform."""
        log.debug(
            f"running terraform command: {command} for definition {definition_name}"
        )
        definition: Definition = self.app_state.definitions[definition_name]

        if self.app_state.terraform_options.provider_cache is None:
            plugin_dir = f"{self._temp_dir}/terraform-plugins"
        else:
            plugin_dir = self.app_state.terraform_options.provider_cache

        color_str = (
            "-no-color" if self.app_state.terraform_options.color is False else ""
        )
        params = {
            "init": f"-input=false {color_str} -plugin-dir={plugin_dir}",
            # -lockfile=readonly is ideal, but many of our modules are not
            # only partially defining the required providers; they need to specify all
            # required providers, or none, and let the worker generate the requirements
            # based on the deployment_config.yaml.j2
            # "init": f"-input=false {color_str} -plugin-dir={plugin_dir} -lockfile=readonly",
            "plan": f"-input=false -detailed-exitcode {color_str}",
            "apply": f"-input=false {color_str} -auto-approve",
            "destroy": f"-input=false {color_str} -auto-approve",
        }
        non_stream_output_func = {
            "init": log.debug,
            "plan": log.info,
            "apply": log.info,
            "destroy": log.info,
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

        for auth in self.app_state.authenticators:
            env.update(auth.env())

        working_dir = definition.get_target_path(
            self.app_state.root_options.working_dir
        )
        command_params = params.get(command)
        if not command_params:
            raise ValueError(
                f"invalid command passed to terraform, {command} has no defined params!"
            )

        # only execute hooks for plan/apply/destroy
        try:
            if hooks.check_hooks("pre", working_dir, command) and command in [
                "apply",
                "destroy",
                "plan",
            ]:
                # pre exec hooks
                # want to pass remotes
                # want to pass tf_vars
                click.secho(
                    f"found pre-{command} hook script for definition {definition.tag},"
                    " executing ",
                    fg="yellow",
                )
                hooks.hook_exec(
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

        log.debug(
            f"cmd: {self.app_state.terraform_options.terraform_bin} {command} {command_params}"
        )
        (exit_code, stdout, stderr) = pipe_exec(
            f"{self.app_state.terraform_options.terraform_bin} {command} {command_params}",
            cwd=working_dir,
            env=env,
            stream_output=self.app_state.terraform_options.stream_output,
        )
        log.debug(f"exit code: {exit_code}")
        # (
        #     self._terraform_output["exit_code"],
        #     self._terraform_output["stdout"],
        #     self._terraform_output["stderr"],
        # ) = (exit_code, stdout, stderr)

        if not self.app_state.terraform_options.stream_output:
            for line in stdout.decode().splitlines():
                non_stream_output_func[command](f"stdout: {line}")
            for line in stderr.decode().splitlines():
                non_stream_output_func[command](f"stderr: {line}")

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
            if hooks.check_hooks("post", working_dir, command) and command in [
                "apply",
                "destroy",
                "plan",
            ]:
                click.secho(
                    f"found post-{command} hook script for definition {definition.tag},"
                    " executing ",
                    fg="yellow",
                )
                hooks.hook_exec(
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
