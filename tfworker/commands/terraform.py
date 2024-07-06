import os
from typing import TYPE_CHECKING, Dict, Union

import tfworker.util.hooks as hooks
import tfworker.util.log as log
import tfworker.util.terraform as tf_util
from tfworker.commands.base import BaseCommand
from tfworker.definitions import Definition
from tfworker.exceptions import HandlerError, HookError, TFWorkerException
from tfworker.types.terraform import TerraformAction, TerraformStage
from tfworker.util.system import pipe_exec

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

            self._exec_terraform_action(name=name, action=TerraformAction.INIT)

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
                if "plan file exists" in reason:
                    self.app_state.definitions[name].needs_apply = True
                log.info(f"definition {name} does not need a plan: {reason}")
                continue

            log.info(f"definition {name} needs a plan: {reason}")
            self._exec_terraform_plan(name=name)

    def terraform_apply_or_destroy(self) -> None:
        if self.app_state.terraform_options.destroy:
            action: TerraformAction = TerraformAction.DESTROY
        elif self.app_state.terraform_options.apply:
            action: TerraformAction = TerraformAction.APPLY
        else:
            log.debug("neither apply nor destroy specified; skipping")
            return

        for name in self.app_state.definitions.keys():
            if action == TerraformAction.DESTROY:
                if self.app_state.terraform_options.limit:
                    if name not in self.app_state.terraform_options.limit:
                        log.info(f"skipping destroy for definition: {name}")
                        continue
            log.trace(
                f"running {action} for definition: {name} if needs_apply is True, value is: {self.app_state.definitions[name].needs_apply}"
            )
            if self.app_state.definitions[name].needs_apply:
                log.info(f"running apply for definition: {name}")
                self._exec_terraform_action(name=name, action=action)

    def _exec_terraform_action(self, name: str, action: TerraformAction) -> None:
        """
        Execute terraform action
        """
        if action == TerraformAction.PLAN:
            raise TFWorkerException(
                "use _exec_terraform_pre_plan & _exec_terraform_plan method to run plan"
            )

        definition: Definition = self.app_state.definitions[name]

        try:
            log.trace(
                f"executing {TerraformStage.PRE} {action.value} handlers for definition {name}"
            )
            self._app_state.handlers.exec_handlers(
                action=action,
                stage=TerraformStage.PRE,
                deployment=self.app_state.deployment,
                definition=definition,
                working_dir=self.app_state.working_dir,
            )
        except HandlerError as e:
            log.error(f"handler error on definition {name}: {e}")
            self.ctx.exit(2)

        log.trace(
            f"executing {TerraformStage.PRE} {action.value} hooks for definition {name}"
        )
        self._exec_hook(
            definition,
            action,
            TerraformStage.PRE,
        )

        log.trace(f"running terraform {action.value} for definition {name}")
        result = self._run(name, action)
        if result.exit_code:
            log.error(f"error running terraform {action.value} for {name}")
            self.ctx.exit(1)

        try:
            log.trace(
                f"executing {TerraformStage.POST.value} {action.value} handlers for definition {name}"
            )
            self._app_state.handlers.exec_handlers(
                action=action,
                stage=TerraformStage.POST,
                deployment=self.app_state.deployment,
                definition=definition,
                working_dir=self.app_state.working_dir,
                result=result,
            )
        except HandlerError as e:
            log.error(f"handler error on definition {name}: {e}")
            self.ctx.exit(2)

        log.trace(
            f"executing {TerraformStage.POST.value} {action.value} hooks for definition {name}"
        )
        self._exec_hook(
            definition,
            action,
            TerraformStage.POST,
            result,
        )

    def _exec_terraform_pre_plan(self, name: str) -> None:
        """
        Execute terraform pre plan with hooks and handlers for the given definition
        """
        definition: Definition = self.app_state.definitions[name]

        log.trace(f"executing pre plan handlers for definition {name}")
        try:
            self._app_state.handlers.exec_handlers(
                action=TerraformAction.PLAN,
                stage=TerraformStage.PRE,
                deployment=self.app_state.deployment,
                definition=definition,
                working_dir=self.app_state.working_dir,
            )
        except HandlerError as e:
            log.error(f"handler error on definition {name}: {e}")
            self.ctx.exit(2)

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

        try:
            log.trace(f"executing post plan handlers for definition {name}")
            self._app_state.handlers.exec_handlers(
                action=TerraformAction.PLAN,
                stage=TerraformStage.POST,
                deployment=self.app_state.deployment,
                definition=definition,
                working_dir=self.app_state.working_dir,
                result=result,
            )
        except HandlerError as e:
            log.error(f"handler error on definition {name}: {e}")
            self.ctx.exit(2)

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

    def has_changes(self) -> bool:
        return self.exit_code == 2


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
