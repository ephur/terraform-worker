import os
import pathlib
from typing import TYPE_CHECKING, Dict, Union

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
from tfworker.types.terraform import TerraformAction, TerraformStage
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

    @property
    def terraform_config(self):
        if hasattr(self, "_terraform_config"):
            return self._terraform_config
        else:
            self._terraform_config = TerraformCommandConfig(self._app_state)
        return self._terraform_config

    def prep_providers(self) -> None:
        """
        Prepare / Mirror the providers
        """
        if self.app_state.terraform_options.provider_cache is None:
            log.debug("no provider cache specified; using temporary cache")
            local_cache = self.app_state.working_dir / "terraform-plugins"
            local_cache.mkdir(exist_ok=True)
            self.app_state.terraform_options.provider_cache = str(local_cache)

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

    def terraform_init(self) -> None:
        from tfworker.definitions.prepare import DefinitionPrepare

        def_prep = DefinitionPrepare(self.ctx, self.app_state)

        for name in self.app_state.definitions.keys():
            log.info(f"initializing definition: {name}")
            def_prep.copy_files(name=name)
            try:
                def_prep.render_templates(name=name)
                def_prep.create_local_vars(name=name)
                def_prep.create_terraform_vars(name=name)
                def_prep.create_worker_tf(name=name)
                def_prep.create_terraform_lockfile(name=name)
            except TFWorkerException as e:
                log.error(f"error rendering templates for definition {name}: {e}")
                self.ctx.exit(1)

            self._execute_terraform_init(name=name)

    def terraform_plan(self) -> None:
        if not self.app_state.terraform_options.plan:
            log.debug("--no-plan option specified; skipping plan")
            return

        from tfworker.definitions.plan import DefinitionPlan

        needed: bool
        reason: str
        def_plan: DefinitionPlan = DefinitionPlan(self.ctx, self.app_state)

        for name in self.app_state.definitions.keys():
            log.info(f"running pre-plan for definition: {name}")
            def_plan.set_plan_file(self.app_state.definitions[name])
            self._exec_terraform_pre_plan(name=name)
            needed, reason = def_plan.needs_plan(self.app_state.definitions[name])

            if not needed:
                log.info(f"definition {name} does not need a plan: {reason}")
                continue

            log.info(f"definition {name} needs a plan: {reason}")
            self._exec_terraform_plan(name=name)

    def _execute_terraform_init(self, name: str) -> None:
        """
        Execute terraform init with hooks and handlers for the given definition
        """
        definition: Definition = self.app_state.definitions[name]

        log.trace(f"executing pre init handlers for definition {name}")
        self._app_state.handlers.exec_handlers(
            action=TerraformAction.INIT,
            stage=TerraformStage.PRE,
            deployment=self.app_state.deployment,
            definition=name,
            working_dir=self.app_state.working_dir,
        )

        log.trace(f"executing pre init hooks for definition {name}")
        self._exec_hook(
            self._app_state.definitions[name],
            TerraformAction.INIT,
            TerraformStage.PRE,
        )

        log.trace(f"running terraform init for definition {name}")
        result = self._run(name, TerraformAction.INIT)
        if result.exit_code:
            log.error(f"error running terraform init for {name}")
            self.ctx.exit(1)

        log.trace(f"executing post init handlers for definition {name}")
        self._app_state.handlers.exec_handlers(
            action=TerraformAction.INIT,
            stage=TerraformStage.POST,
            deployment=self.app_state.deployment,
            definition=definition,
            working_dir=self.app_state.working_dir,
            result=result,
        )

        log.trace(f"executing post init hooks for definition {name}")
        self._exec_hook(
            self._app_state.definitions[name],
            TerraformAction.INIT,
            TerraformStage.POST,
            result,
        )

    def _exec_terraform_pre_plan(self, name: str) -> None:
        """
        Execute terraform pre plan with hooks and handlers for the given definition
        """
        definition: Definition = self.app_state.definitions[name]

        log.trace(f"executing pre plan handlers for definition {name}")
        self._app_state.handlers.exec_handlers(
            action=TerraformAction.PLAN,
            stage=TerraformStage.PRE,
            deployment=self.app_state.deployment,
            definition=definition,
            working_dir=self.app_state.working_dir,
        )

        log.trace(f"executing pre plan hooks for definition {name}")
        self._exec_hook(
            self._app_state.definitions[name],
            TerraformAction.PLAN,
            TerraformStage.PRE,
        )

    def _exec_terraform_plan(self, name: str) -> None:
        """
        Execute terraform plan with hooks and handlers for the given definition
        """
        definition: Definition = self.app_state.definitions[name]

        log.trace(f"running terraform plan for definition {name}")
        result = self._run(name, TerraformAction.PLAN)

        if result.exit_code == 0:
            log.debug(f"no changes for definition {name}")
            definition.needs_apply = False

        if result.exit_code == 1:
            log.error(f"error running terraform plan for {name}")
            self.ctx.exit(1)

        if result.exit_code == 2:
            log.debug(f"terraform plan for {name} indicates changes")
            definition.needs_apply = True

        log.trace(f"executing post plan handlers for definition {name}")
        self._app_state.handlers.exec_handlers(
            action=TerraformAction.PLAN,
            stage=TerraformStage.POST,
            deployment=self.app_state.deployment,
            definition=definition,
            working_dir=self.app_state.working_dir,
            result=result,
        )

        log.trace(f"executing post plan hooks for definition {name}")
        self._exec_hook(
            self._app_state.definitions[name],
            TerraformAction.PLAN,
            TerraformStage.POST,
            result,
        )

    def _run(
        self,
        definition_name: str,
        action: TerraformAction,
    ) -> "TerraformResult":
        """
        run terraform
        """
        log.debug(
            f"handling terraform command: {action} for definition {definition_name}"
        )
        definition: Definition = self.app_state.definitions[definition_name]
        params: dict = self.terraform_config.get_params(
            action, plan_file=definition.plan_file
        )

        working_dir: str = definition.get_target_path(
            self.app_state.root_options.working_dir
        )

        log.debug(
            f"cmd: {self.app_state.terraform_options.terraform_bin} {action} {params}"
        )

        result: TerraformResult = TerraformResult(
            *pipe_exec(
                f"{self.app_state.terraform_options.terraform_bin} {action} {params}",
                cwd=working_dir,
                env=self.terraform_config.env,
                stream_output=self.terraform_config.stream_output,
            )
        )

        log.debug(f"exit code: {result.exit_code}")
        return result

    def _exec_hook(
        self,
        definition: Definition,
        action: TerraformAction,
        stage: TerraformStage,
        result: Union["TerraformResult", None] = None,
    ) -> None:
        """
        Find and execute the appropriate hooks for a supplied definition

        Args:
            definition (Definition): the definition to execute the hooks for
            action (TerraformAction): the action to execute the hooks for
            stage (TerraformStage): the stage to execute the hooks for
            result (TerraformResult): the result of the terraform command
        """
        hook_dir = definition.get_target_path(self.app_state.working_dir)

        try:
            if not hooks.check_hooks(stage, hook_dir, action):
                log.trace(
                    f"no {stage}-{action} hooks found for definition {definition.name}"
                )
                return

            log.info(
                f"executing {stage}-{action} hooks for definition {definition.name}"
            )
            hooks.hook_exec(
                stage,
                action,
                hook_dir,
                self.terraform_config.env,
                self.terraform_config.terraform_bin,
                b64_encode=self.terraform_config.b64_encode,
                debug=self.terraform_config.debug,
                extra_vars=definition.get_template_vars(
                    self.app_state.loaded_config.global_vars.template_vars
                ),
            )
        except HookError as e:
            log.error(f"hook execution error on definition {definition.name}: \n{e}")
            self.ctx.exit(2)


class TerraformResult:
    """
    Hold the results of a terraform run
    """

    def __init__(self, exit_code: int, stdout: bytes, stderr: bytes):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    @property
    def stdout_str(self) -> str:
        return self.stdout.decode()

    @property
    def stderr_str(self) -> str:
        return self.stdout.decode()

    def log_stdout(self, action: TerraformAction) -> None:
        log_method = TerraformCommandConfig.get_config().get_log_method(action)
        for line in self.stdout.decode().splitlines():
            log_method(f"stdout: {line}")

    def log_stderr(self, action: TerraformAction) -> None:
        log_method = TerraformCommandConfig.get_config().get_log_method(action)
        for line in self.stderr.decode().splitlines():
            log_method(f"stderr: {line}")

    def log_file(self, filename: str) -> None:
        with open(filename, "w+") as f:
            f.write(self.stdout.decode())
            f.write(self.stderr.decode())


class TerraformCommandConfig:
    """
    A class to hold parameters for terraform commands

    this class is meant to be a singleton
    """

    _instance = None

    def __new__(cls, app_state: "AppState"):
        if cls._instance is None:
            cls._instance = super(TerraformCommandConfig, cls).__new__(cls)
            cls._instance._app_state = app_state
        return cls._instance

    def __init__(self, app_state: "AppState"):
        self._app_state = app_state
        self._env = None

    @classmethod
    def get_config(cls) -> "TerraformCommandConfig":
        return cls._instance

    @property
    def stream_output(self):
        return self._app_state.terraform_options.stream_output

    @property
    def terraform_bin(self):
        return self._app_state.terraform_options.terraform_bin

    @property
    def env(self):
        if self._env is None:
            self._env = self._get_env()
        return self._env

    @property
    def b64_encode(self):
        return self._app_state.terraform_options.b64_encode

    @property
    def debug(self):
        if (
            log.LogLevel[self._app_state.root_options.log_level].value
            <= log.LogLevel.DEBUG.value
        ):
            return True
        return False

    @property
    def action(self):
        if self._app_state.terraform_options.destroy:
            return TerraformAction.DESTROY
        return TerraformAction.APPLY

    @staticmethod
    def get_log_method(command: str) -> callable:
        return {
            "init": log.debug,
            "plan": log.info,
            "apply": log.info,
            "destroy": log.info,
        }[command]

    def get_params(self, command: TerraformAction, plan_file: str) -> str:
        """Return the parameters for a given command"""
        color_str = (
            "-no-color" if self._app_state.terraform_options.color is False else ""
        )

        plan_action = " -destroy" if self.action == TerraformAction.DESTROY else ""

        """ it would be desirable to add -lockfile=readonly but requires modules have strict
            adherance to defining all module versions
            "init": f"-input=false {color_str} -plugin-dir={plugin_dir} -lockfile=readonly"""
        return {
            TerraformAction.INIT: f"-input=false {color_str} -plugin-dir={self._app_state.terraform_options.provider_cache}",
            TerraformAction.PLAN: f"-input=false {color_str} {plan_action} -detailed-exitcode -out {plan_file}",
            TerraformAction.APPLY: f"-input=false {color_str} -auto-approve {plan_file}",
            TerraformAction.DESTROY: f"-input=false {color_str} -auto-approve",
        }[command]

    def _get_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        # acknowledge that we are using a plugin cache; and compute the lockfile each run
        env["TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE"] = "1"
        # reduce non essential terraform output
        env["TF_IN_AUTOMATION"] = "1"

        for auth in self._app_state.authenticators:
            env.update(auth.env())
        return env

    ###################################################################################################
    ###################################################################################################
    ###################################################################################################
    ###################################################################################################
    ###################################################################################################
    ###################################################################################################
    ###################################################################################################
    ###################################################################################################
    ###################################################################################################
    ###################################################################################################
    ###################################################################################################
    ###################################################################################################
    ###################################################################################################
    ###################################################################################################
    ###################################################################################################
    ###################################################################################################

    ###########################################
    # Methods for dealing with terraform plan #
    ###########################################
    # def _check_plan(self, definition: Definition) -> bool:
    #     """
    #     Determines if a plan is needed for the provided definition

    #     Args:
    #         definition: the definition to check for a plan

    #     Returns:
    #         bool: True if a plan is needed, False otherwise
    #     """
    #     if not self._plan_file_path:
    #         return self._handle_no_plan_path(definition)

    #     plan_file = self._prepare_plan_file(definition)
    #     self._validate_plan_path(plan_file.parent)
    #     self._run_handlers(definition, "plan", "check", plan_file=plan_file)

    #     return self._should_plan(definition, plan_file)

    # def _handle_no_plan_path(self, definition: Definition) -> bool:
    #     """Handles the case where no plan path is specified, saved plans are not possible

    #     Args:
    #         definition: the definition to check for a plan

    #     Returns:
    #         bool: True if a plan is needed, False otherwise
    #     """

    #     if not self._tf_plan:
    #         definition._ready_to_apply = True
    #         return False
    #     definition._ready_to_apply = False
    #     return True

    # def _prepare_plan_file(self, definition: Definition) -> pathlib.Path:
    #     """Prepares the plan file for the definition

    #     Args:
    #         definition: the definition to prepare the plan file for

    #     Returns:
    #         pathlib.Path: the path to the plan file
    #     """
    #     plan_path = pathlib.Path(self._plan_file_path).resolve()
    #     plan_file = plan_path / f"{self._deployment}_{definition.tag}.tfplan"
    #     definition.plan_file = plan_file
    #     click.secho(f"using plan file:{plan_file}", fg="yellow")
    #     return plan_file

    # def _validate_plan_path(self, plan_path: pathlib.Path) -> None:
    #     """Validates the plan path

    #     Args:
    #         plan_path: the path to the plan file

    #     Returns:
    #         None
    #     """
    #     if not (plan_path.exists() and plan_path.is_dir()):
    #         click.secho(
    #             f'plan path "{plan_path}" is not suitable, it is not an existing directory'
    #         )
    #         raise SystemExit(1)

    # def _exec_plan(self, definition) -> bool:
    #     """_exec_plan executes a terraform plan, returns true if a plan has changes"""
    #     changes = False

    #     # call handlers for pre plan
    #     try:
    #         self._execute_handlers(
    #             action="plan",
    #             stage="pre",
    #             deployment=self._deployment,
    #             definition=definition.tag,
    #             definition_path=definition.fs_path,
    #         )
    #     except HandlerError as e:
    #         if e.terminate:
    #             click.secho(f"terminating due to fatal handler error {e}", fg="red")
    #             raise SystemExit(1)
    #         click.secho(f"handler error: {e}", fg="red")

    #     click.secho(
    #         f"planning definition for {self._plan_for}: {definition.tag}",
    #         fg="green",
    #     )

    #     try:
    #         self._run(
    #             definition,
    #             "plan",
    #             debug=self._show_output,
    #             plan_action=self._plan_for,
    #             plan_file=str(definition.plan_file),
    #         )
    #     except PlanChange:
    #         # on destroy, terraform ALWAYS indicates a plan change
    #         click.secho(f"plan changes for {self._plan_for} {definition.tag}", fg="red")
    #         definition._ready_to_apply = True
    #         changes = True
    #     except TerraformError:
    #         click.secho(
    #             f"error planning terraform definition: {definition.tag}!",
    #             fg="red",
    #         )
    #         raise SystemExit(2)

    #     try:
    #         self._execute_handlers(
    #             action="plan",
    #             stage="post",
    #             deployment=self._deployment,
    #             definition=definition.tag,
    #             definition_path=definition.fs_path,
    #             text=strip_ansi(self._terraform_output["stdout"].decode()),
    #             planfile=definition.plan_file,
    #             changes=changes,
    #         )
    #     except HandlerError as e:
    #         click.secho(f"{e}", fg="red")
    #         if e.terminate:
    #             click.secho("error is fatal, terminating", fg="red")
    #             raise SystemExit(1)

    #     if not changes:
    #         click.secho(f"no plan changes for {definition.tag}", fg="yellow")

    #     return changes

    # def _should_plan(self, definition: Definition, plan_file: pathlib.Path) -> bool:
    #     if not self._tf_plan:
    #         definition._ready_to_apply = True
    #         return False

    #     if plan_file.exists():
    #         if plan_file.stat().st_size == 0:
    #             click.secho(
    #                 f"exiting plan file {plan_file} exists but is empty; planning again",
    #                 fg="green",
    #             )
    #             definition._ready_to_apply = False
    #             return True
    #         click.secho(
    #             f"existing plan file {plan_file} is suitable for apply; not planning again; remove plan file to allow planning",
    #             fg="green",
    #         )
    #         definition._ready_to_apply = True
    #         return False

    #     definition._ready_to_apply = False
    #     return True

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
            self.app_state.handlers.execute_handlers(
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
            self.app_state.handlers.exec_handlers(
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
    # def _run_handlers(
    #     self, definition, action, stage, plan_file=None, **kwargs
    # ) -> None:
    #     """Runs the handlers for the given action and stage

    #     Args:
    #         definition: the definition to run the handlers for
    #         action: the action to run the handlers for
    #         stage: the stage to run the handlers for
    #         plan_file: the plan file to pass to the handlers
    #         kwargs: additional keyword arguments to pass to the handlers

    #     Returns:
    #         None
    #     """
    #     try:
    #         self._execute_handlers(
    #             action=action,
    #             stage=stage,
    #             deployment=self._deployment,
    #             definition=definition.tag,
    #             definition_path=definition.fs_path,
    #             planfile=plan_file,
    #             **kwargs,
    #         )
    #     except HandlerError as e:
    #         if e.terminate:
    #             click.secho(f"terminating due to fatal handler error {e}", fg="red")
    #             raise SystemExit(1)
    #         click.secho(f"handler error: {e}", fg="red")
