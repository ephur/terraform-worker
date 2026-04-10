import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from shlex import quote as shlex_quote
from typing import TYPE_CHECKING, Dict, Union

import tfworker.util.hooks as hooks
import tfworker.util.log as log
import tfworker.util.terraform as tf_util
from tfworker.commands.base import BaseCommand
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.definitions import Definition
from tfworker.exceptions import HandlerError, HookError, TFWorkerException
from tfworker.util.system import pipe_exec
from tfworker.util.terraform import quote_index_brackets

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

    def _get_definitions_needing_init(self) -> list[str]:
        """
        Determine which definitions need initialization.

        Standard workflow: init -> plan -> apply
        With --no-plan: init -> apply (but apply is skipped if no plan exists)

        Optimization: In --no-plan mode, if no plan is available, both init and apply
        will be effectively skipped, so we can skip the time-consuming init entirely.

        Returns:
            list[str]: List of definition names that need initialization
        """
        all_definition_names = list(self.app_state.definitions.keys())

        # If planning is enabled, all definitions need init (for plan generation)
        if (
            self.app_state.terraform_options.plan
            or self.app_state.terraform_options.plan_destroy
        ):
            return all_definition_names

        # If NOT in apply mode, all definitions need init
        if not self.app_state.terraform_options.apply:
            return all_definition_names

        # In apply-only mode, skip init for definitions that have NO plans
        # (since apply will be skipped anyway without a plan)
        from tfworker.definitions.plan import DefinitionPlan

        def_plan = DefinitionPlan(self.ctx, self.app_state)
        definitions_needing_init = []

        for name in all_definition_names:
            definition = self.app_state.definitions[name]

            # Set up plan file path so we can check if it exists
            def_plan.set_plan_file(definition)

            # Check if we have a plan available for this definition
            has_handler_plan = self.app_state.handlers.has_available_plan(definition)
            has_local_plan = definition.existing_planfile(self.app_state.working_dir)

            if has_handler_plan or has_local_plan:
                # We have a plan, need init for apply-only mode
                definitions_needing_init.append(name)
                if has_handler_plan:
                    log.info(
                        f"Will init definition {name}: apply-only mode with plan available from handler"
                    )
                else:
                    log.info(
                        f"Will init definition {name}: apply-only mode with existing local plan file"
                    )
                # Mark as needing apply since we have a plan
                definition.needs_apply = True
            else:
                # No plan available, skip init (and apply will be skipped too)
                log.info(
                    f"Skipping init for definition {name}: apply-only mode with no plan available"
                )

        if not definitions_needing_init:
            log.info(
                "No definitions have plans available, skipping all init for apply-only mode"
            )

        return definitions_needing_init

    def terraform_init(self) -> None:
        from tfworker.definitions.prepare import DefinitionPrepare

        def_prep = DefinitionPrepare(self.app_state)
        definition_names = self._get_definitions_needing_init()

        if not definition_names:
            return

        # Use sequential processing for small numbers of definitions
        if len(definition_names) < 4:
            log.info(f"Initializing {len(definition_names)} definitions sequentially")
            for name in definition_names:
                log.info(f"initializing definition: {name}")
                try:
                    self._prepare_definition(def_prep, name)
                    self._terraform_init_single(name)
                except TFWorkerException as e:
                    log.error(f"Error with definition {name}: {e}")
                    self.ctx.exit(1)
            return

        log.info(f"Initializing {len(definition_names)} definitions in parallel")

        # Phase 1: Prepare all definitions in parallel (file operations)
        log.info("Phase 1: Preparing definition files in parallel")
        with ThreadPoolExecutor(
            max_workers=self.app_state.loaded_config.parallel_options.max_preparation_workers
        ) as executor:
            prepare_futures = []
            for name in definition_names:
                future = executor.submit(self._prepare_definition, def_prep, name)
                prepare_futures.append((name, future))

            # Wait for all preparations to complete
            for name, future in prepare_futures:
                try:
                    future.result()
                    log.debug(f"Completed preparation for definition: {name}")
                except Exception as e:
                    log.error(f"Error preparing definition {name}: {e}")
                    self.ctx.exit(1)

        # Phase 2: Run terraform init in parallel (smaller pool)
        log.info("Phase 2: Running terraform init in parallel")

        # Force no streaming output for parallel execution
        self.terraform_config.force_no_stream_output()

        try:
            with ThreadPoolExecutor(
                max_workers=self.app_state.loaded_config.parallel_options.max_init_workers
            ) as executor:
                init_futures = []
                for name in definition_names:
                    future = executor.submit(self._terraform_init_single, name)
                    init_futures.append((name, future))

                # Collect results and handle completions
                show_output = self._app_state.terraform_options.stream_output
                self._handle_parallel_init_results(init_futures, show_output)
        finally:
            # Clear the stream output override
            self.terraform_config.clear_stream_output_override()

        log.info("All definitions initialized successfully")

    def terraform_plan(self) -> None:
        from tfworker.definitions.plan import DefinitionPlan

        def_plan: DefinitionPlan = DefinitionPlan(self.ctx, self.app_state)
        needed: bool
        reason: str

        # check for existing plan files that need an apply; do this before skipping
        # the plan, still need to ensure if they are ready for an apply
        for name in self.app_state.definitions.keys():
            def_plan.set_plan_file(self.app_state.definitions[name])
            needed, reason = def_plan.needs_plan(self.app_state.definitions[name])
            if not needed:
                if "plan file exists" in reason:
                    for name in self.app_state.definitions.keys():
                        self.app_state.definitions[name].needs_apply = True

        # if --no-plan and --no-plan-destroy are specified, skip the plan regardless
        if (
            not self.app_state.terraform_options.plan
            and not self.app_state.terraform_options.plan_destroy
        ):
            log.debug("--no-plan option specified; skipping plan execution")
            return

        for name in self.app_state.definitions.keys():
            log.info(f"running pre-plan for definition: {name}")
            self._exec_terraform_pre_plan(name=name)
            needed, reason = def_plan.needs_plan(self.app_state.definitions[name])
            if not needed:
                log.info(f"Plan not needed for definition: {name}, reason: {reason}")
                continue

            log.info(f"definition {name} needs a plan: {reason}")
            self._exec_terraform_plan(name=name)
            if getattr(self.app_state.definitions[name], "always_apply", False):
                log.info(
                    f"definition {name} has always_apply set; applying immediately after plan"
                )
                self._exec_terraform_action(name=name, action=TerraformAction.APPLY)
                self.app_state.definitions[name].needs_apply = False

    def terraform_apply_or_destroy(self) -> None:
        log.trace("entering terraform apply or destroy")

        if self.app_state.terraform_options.destroy:
            action: TerraformAction = TerraformAction.DESTROY
        elif self.app_state.terraform_options.apply:
            action: TerraformAction = TerraformAction.APPLY
        else:
            log.debug("neither apply nor destroy specified; skipping")
            return

        for name in self.app_state.definitions.keys():
            log.trace(f"running {action} for definition: {name}")
            if action == TerraformAction.DESTROY:
                if self.app_state.terraform_options.limit:
                    if name not in self.app_state.terraform_options.limit:
                        log.info(f"skipping destroy for definition: {name}")
                        continue
            log.trace(
                f"running {action} for definition: {name} if needs_apply is True, "
                f"needs_apply value: {self.app_state.definitions[name].needs_apply}"
            )
            if self.app_state.definitions[name].needs_apply:
                plan_file = self._app_state.definitions[name].plan_file
                if plan_file is None or not Path(plan_file).exists():
                    log.info(
                        f"plan file does not exist for definition: {name}; skipping apply"
                    )
                    continue
                log.info(f"running {action} for definition: {name}")
                self._exec_terraform_action(name=name, action=action)

    def _handle_parallel_init_results(self, init_futures, show_output: bool) -> None:
        """
        Handle results from parallel terraform init execution.

        Args:
            init_futures: List of (name, future) tuples from parallel execution
            show_output: Whether to show successful terraform output (based on original stream_output setting)
        """
        for name, future in init_futures:
            try:
                result = future.result()
                log.debug(f"Completed terraform init for definition: {name}")

                # Log successful output only if original stream_output was enabled
                if show_output and result and not log.json_logging_enabled():
                    self._log_terraform_result(name, result)

            except (TFWorkerException, KeyError) as e:
                log.error(f"Error initializing definition {name}: {e}")
                self.ctx.exit(1)

    def _log_terraform_result(self, name: str, result: "TerraformResult") -> None:
        """
        Log terraform result output with definition name prefix.

        Args:
            name: Definition name for log prefixing
            result: TerraformResult containing stdout/stderr to log
        """
        if result.stdout:
            for line in result.stdout.decode().strip().split("\n"):
                if line.strip():
                    log.info(f"[{name}] {line}")
        if result.stderr:
            for line in result.stderr.decode().strip().split("\n"):
                if line.strip():
                    log.info(f"[{name}] stderr: {line}")

    def _prepare_definition(self, def_prep, name: str) -> None:
        """Prepare a single definition for terraform init"""
        log.trace(f"preparing definition: {name}")
        def_prep.copy_files(name=name)
        try:
            def_prep.render_templates(name=name)
            def_prep.create_local_vars(name=name)
            def_prep.create_terraform_vars(name=name)
            def_prep.create_worker_tf(name=name)
            def_prep.download_modules(
                name=name,
                stream_output=False,  # Disable streaming for parallel execution
            )
            def_prep.create_terraform_lockfile(name=name)
        except TFWorkerException as e:
            raise TFWorkerException(f"error preparing definition {name}: {e}") from e

    def _terraform_init_single(self, name: str) -> "TerraformResult":
        """Run terraform init for a single definition"""
        log.trace(f"running terraform init for definition: {name}")
        return self._exec_terraform_action(name=name, action=TerraformAction.INIT)

    def _exec_terraform_action(
        self, name: str, action: TerraformAction
    ) -> "TerraformResult":
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
            # If stream_output was disabled, show the captured output at error level
            if (
                not self.terraform_config.stream_output
                and not log.json_logging_enabled()
            ):
                if result.stdout:
                    for line in result.stdout.decode().strip().split("\n"):
                        if line.strip():
                            log.error(f"[{name}] {line}")
                if result.stderr:
                    for line in result.stderr.decode().strip().split("\n"):
                        if line.strip():
                            log.error(f"[{name}] stderr: {line}")
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

        if action == TerraformAction.APPLY:
            if definition.plan_file is not None:
                Path(definition.plan_file).unlink(missing_ok=True)

        return result

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

        if self._app_state.terraform_options.target:
            log.info(
                f"targeting resources: {', '.join(self._app_state.terraform_options.target)}"
            )

        result = self._run(name, TerraformAction.PLAN)

        if result.exit_code == 0:
            if definition.always_apply:
                log.debug(f"no changes for definition {name}; but always_apply is set")
                result.exit_code = 2
            else:
                log.debug(
                    f"no changes for definition {name}; not applying and removing plan file"
                )
                definition.needs_apply = False
                definition.plan_file.unlink(missing_ok=True)

        if result.exit_code == 1:
            log.error(f"error running terraform plan for {name}")
            self.ctx.exit(1)

        if result.exit_code == 2:
            log.debug(f"terraform plan for {name} indicates changes")
            definition.needs_apply = True
            self._generate_plan_output_json(name)

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

    def _generate_plan_output_json(self, name) -> None:
        """
        Generate a plan file in JSON format using the binary plan_file
        """
        log.debug(f"generating JSON plan file for {name}")
        definition: Definition = self.app_state.definitions[name]

        working_dir: str = definition.get_target_path(
            self.app_state.root_options.working_dir
        )

        result: TerraformResult = TerraformResult(
            *pipe_exec(
                f"{self.app_state.terraform_options.terraform_bin} show -json {definition.plan_file}",
                cwd=working_dir,
                env=self.terraform_config.env,
                stream_output=False,
            )
        )

        planfile = Path(definition.plan_file)
        jsonfile = planfile.with_suffix(".tfplan.json")

        log.trace(f"writing json plan file {jsonfile}")
        result.log_file(jsonfile.resolve())

    def _run(
        self,
        definition_name: str,
        action: TerraformAction,
    ) -> "TerraformResult":
        """
        run terraform
        """
        definition: Definition = self.app_state.definitions[definition_name]
        stream_output: bool = self.terraform_config.stream_output
        params: str = self.terraform_config.get_params(
            action, plan_file=definition.plan_file
        )

        working_dir: str = definition.get_target_path(
            self.app_state.root_options.working_dir
        )

        # For DESTROY action, use "apply" command since terraform requires
        # "terraform apply planfile" for both regular and destroy plans
        terraform_command = "apply" if action == TerraformAction.DESTROY else action

        log.debug(
            f"handling terraform {action} action for definition {definition_name}"
        )
        log.info(
            f"running cmd: {self.app_state.terraform_options.terraform_bin} {terraform_command} {params}"
        )

        if definition.squelch_apply_output and action == TerraformAction.APPLY:
            log.debug(
                f"squelching output for apply command on definition {definition_name}"
            )
            stream_output = False
        elif definition.squelch_plan_output and action == TerraformAction.PLAN:
            log.debug(
                f"squelching output for plan command on definition {definition_name}"
            )
            stream_output = False

        aggregate_output = log.json_logging_enabled()
        effective_stream_output = stream_output and not aggregate_output

        pipe_exec_kwargs = {
            "cwd": working_dir,
            "env": self.terraform_config.env,
            "stream_output": effective_stream_output,
        }
        if effective_stream_output:
            pipe_exec_kwargs["stream_log_level"] = log.LogLevel.INFO
            pipe_exec_kwargs["stream_log_context"] = {
                "source": "subprocess",
                "stream": "combined",
                "command": f"terraform {terraform_command}",
                "definition": definition_name,
                "terraform_action": action.value,
            }

        result: TerraformResult = TerraformResult(
            *pipe_exec(
                f"{self.app_state.terraform_options.terraform_bin} {terraform_command} {params}",
                **pipe_exec_kwargs,
            )
        )

        if aggregate_output:
            # For terraform plan, exit code 2 means changes detected (not an error)
            # For other commands, any non-zero exit code is an error
            if action == TerraformAction.PLAN and result.exit_code == 2:
                log_level = log.LogLevel.INFO
            elif result.exit_code != 0:
                log_level = log.LogLevel.ERROR
            else:
                log_level = log.LogLevel.INFO
            log.log_subprocess_result(
                command=f"terraform {terraform_command}",
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                level=log_level,
                extra={
                    "definition": definition_name,
                    "terraform_action": action.value,
                },
                message=f"terraform {terraform_command} output for {definition_name}",
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
                disable_remote_state_vars=definition.hooks_disable_remotes,
                extra_vars=definition.get_template_vars(
                    self.app_state.loaded_config.global_vars.template_vars
                ),
                backend=self.app_state.backend,
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
        self._force_no_stream_output = False

    @classmethod
    def get_config(cls) -> "TerraformCommandConfig":
        return cls._instance

    def force_no_stream_output(self) -> None:
        """Force stream_output to False (used during parallel execution)"""
        self._force_no_stream_output = True

    def clear_stream_output_override(self) -> None:
        """Clear the forced stream_output override"""
        self._force_no_stream_output = False

    @property
    def stream_output(self):
        if self._force_no_stream_output:
            return False
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
        if (
            self._app_state.terraform_options.destroy
            or self._app_state.terraform_options.plan_destroy
        ):
            return TerraformAction.DESTROY
        return TerraformAction.APPLY

    @property
    def strict_locking(self):
        return self._app_state.terraform_options.strict_locking

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
        read_only = "-lockfile=readonly" if self.strict_locking else ""

        target_args = ""
        if self._app_state.terraform_options.target:
            if command != TerraformAction.PLAN:
                log.warn(
                    f"--target option is only valid for plan, ignoring for {command.value}"
                )
            else:
                target_args = " " + " ".join(
                    f"-target={shlex_quote(quote_index_brackets(target))}"
                    for target in self._app_state.terraform_options.target
                )

        return {
            TerraformAction.INIT: f"-input=false {color_str} {read_only} -plugin-dir={self._app_state.terraform_options.provider_cache}",
            TerraformAction.PLAN: f"-input=false {color_str} {plan_action} -detailed-exitcode -out {plan_file}{target_args}",
            TerraformAction.APPLY: f"-input=false {color_str} -auto-approve {plan_file}",
            TerraformAction.DESTROY: f"-input=false {color_str} -auto-approve {plan_file}",
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
