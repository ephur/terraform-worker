import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from shlex import quote as shlex_quote
from typing import TYPE_CHECKING, Dict, List, Union

import click

import tfworker.util.hooks as hooks
import tfworker.util.log as log
import tfworker.util.terraform as tf_util
from tfworker.commands.base import BaseCommand
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.definitions import Definition
from tfworker.exceptions import HandlerError, HookError, TFWorkerException
from tfworker.util.system import pipe_exec, pipe_exec_logged
from tfworker.util.terraform import quote_index_brackets

if TYPE_CHECKING:
    from tfworker.app_state import AppState
    from tfworker.definitions.plan import DefinitionPlan


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

    @property
    def init_errors(self) -> Dict[str, str]:
        """Failure message per definition that could not be prepared/initialized."""
        if not hasattr(self, "_init_errors"):
            self._init_errors: Dict[str, str] = {}
        return self._init_errors

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
        """
        Prepare and initialize all definitions that require it.

        Preparation (copying files, rendering templates, `terraform get`) and
        `terraform init` are treated as a single "init" phase; a failure in
        either marks the definition as failed rather than terminating the run
        on the spot. Two options govern the phase, mirroring the plan options:

        - `init_failures`: halt the phase when a definition fails, leaving the
          remaining definitions uninitialized (default, matches prior behavior)
        - `fail_on_init_error`: exit non-zero once the phase completes if any
          definition failed (default)

        With `--no-init-failures` every definition is attempted, so a single
        run reports every failure instead of one at a time.
        """
        from tfworker.definitions.prepare import DefinitionPrepare

        def_prep = DefinitionPrepare(self.app_state)
        definition_names = self._get_definitions_needing_init()

        if not definition_names:
            return

        # created before any worker threads can race to create it
        self.init_errors

        # Use sequential processing for small numbers of definitions
        if len(definition_names) < 4:
            log.info(f"Initializing {len(definition_names)} definitions sequentially")
            self._init_sequential(def_prep, definition_names)
        else:
            log.info(f"Initializing {len(definition_names)} definitions in parallel")
            self._init_parallel(def_prep, definition_names)

        self._report_init_results(definition_names)

    def _init_sequential(self, def_prep, definition_names: List[str]) -> None:
        """
        Prepare and init definitions one at a time.

        When `init_failures` is set the loop stops at the first failure and the
        definitions it never reached are marked as skipped.
        """
        halt_on_failure = self.app_state.terraform_options.init_failures

        for index, name in enumerate(definition_names):
            log.info(f"initializing definition: {name}")
            # short circuits: a definition that did not prepare is not inited
            succeeded = self._prepare_definition_guarded(
                def_prep, name
            ) and self._init_definition_guarded(name)
            if not succeeded and halt_on_failure:
                self._mark_init_skipped(definition_names[index + 1 :])
                return

    def _init_parallel(self, def_prep, definition_names: List[str]) -> None:
        """
        Prepare and init definitions with worker pools.

        Every definition is submitted to each phase, so all preparation
        failures (a bad module source is the common case) surface in a single
        run. `init_failures` is honored between the phases: when a preparation
        fails, terraform init is not run for the definitions that prepared
        cleanly, and they are marked as skipped.
        """
        halt_on_failure = self.app_state.terraform_options.init_failures

        # Phase 1: Prepare all definitions in parallel (file operations)
        log.info("Phase 1: Preparing definition files in parallel")
        with ThreadPoolExecutor(
            max_workers=self.app_state.loaded_config.parallel_options.max_preparation_workers
        ) as executor:
            prepare_futures = [
                (
                    name,
                    executor.submit(self._prepare_definition_guarded, def_prep, name),
                )
                for name in definition_names
            ]

            # Wait for all preparations to complete
            prepared = [name for name, future in prepare_futures if future.result()]

        if len(prepared) != len(definition_names) and halt_on_failure:
            self._mark_init_skipped(prepared)
            return

        if not prepared:
            return

        # Phase 2: Run terraform init in parallel (smaller pool)
        log.info("Phase 2: Running terraform init in parallel")

        # Force no streaming output for parallel execution
        self.terraform_config.force_no_stream_output()

        try:
            with ThreadPoolExecutor(
                max_workers=self.app_state.loaded_config.parallel_options.max_init_workers
            ) as executor:
                init_futures = [
                    (name, executor.submit(self._terraform_init_single, name))
                    for name in prepared
                ]

                # Collect results and handle completions
                show_output = self._app_state.terraform_options.stream_output
                self._handle_parallel_init_results(init_futures, show_output)
        finally:
            # Clear the stream output override
            self.terraform_config.clear_stream_output_override()

    def _report_init_results(self, definition_names: List[str]) -> None:
        """
        Report the outcome of the init phase, and exit if it is fatal.

        Every failure of the phase is reported together here; the exit code is
        governed by `fail_on_init_error` so a run can be configured to gather
        all failures and still continue.
        """
        failed = [
            name
            for name in definition_names
            if self.app_state.definitions[name].init_failed
        ]
        skipped = [
            name
            for name in definition_names
            if self.app_state.definitions[name].init_skipped
        ]

        if not failed:
            log.info("All definitions initialized successfully")
            return

        log.error(
            f"{len(failed)} of {len(definition_names)} definitions failed to initialize:"
        )
        for name in failed:
            log.error(f"  {name}: {self.init_errors.get(name, 'unknown error')}")
        if skipped:
            log.warn(
                f"{len(skipped)} definitions were not initialized: {', '.join(skipped)}"
            )

        if self.app_state.terraform_options.fail_on_init_error:
            self.ctx.exit(1)

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
                    self.app_state.definitions[name].needs_apply = True

        # if --no-plan and --no-plan-destroy are specified, skip the plan regardless
        if (
            not self.app_state.terraform_options.plan
            and not self.app_state.terraform_options.plan_destroy
        ):
            # a saved plan is fetched by the plan phase, which this run does not
            # have; without this an apply-only run has nothing to apply
            self._fetch_saved_plans(def_plan)
            # info, not debug: this ends the phase without touching a single
            # definition, and a run that quietly does nothing is indistinguishable
            # from a run that died
            log.info(
                "no plan requested (--no-plan and --no-plan-destroy); "
                "no definitions will be planned"
            )
            return

        for name in self.app_state.definitions.keys():
            if self._init_incomplete(name):
                log.warn(
                    f"skipping plan for definition: {name}; it was not initialized"
                )
                continue
            log.info(f"running pre-plan for definition: {name}")
            if not self._exec_terraform_pre_plan(name=name):
                if self.app_state.terraform_options.plan_failures:
                    break
                continue
            needed, reason = def_plan.needs_plan(self.app_state.definitions[name])
            if not needed:
                log.info(f"Plan not needed for definition: {name}, reason: {reason}")
                continue
            log.info(f"definition {name} needs a plan: {reason}")
            self._exec_terraform_plan(name=name)
            if (
                self.app_state.definitions[name].plan_failed
                and self.app_state.terraform_options.plan_failures
            ):
                break
            if not self.app_state.definitions[name].plan_failed and getattr(
                self.app_state.definitions[name], "always_apply", False
            ):
                log.info(
                    f"definition {name} has always_apply set; applying immediately after plan"
                )
                self._exec_terraform_action(name=name, action=TerraformAction.APPLY)
                self.app_state.definitions[name].needs_apply = False

        if self.app_state.terraform_options.fail_on_plan_error:
            if any(d.plan_failed for d in self.app_state.definitions.values()):
                self.ctx.exit(1)

    def _fetch_saved_plans(self, def_plan: "DefinitionPlan") -> None:
        """
        Retrieve the plans a previous run saved so an apply-only run can use them.

        Only the handler holding the plan is asked for it; the pre-plan stage is
        not dispatched, as handlers such as snyk and trivy hook it to scan and a
        run that is not planning should not trigger them.

        A definition whose plan is already on disk is left alone: that is the
        local plan file the `plan_file_path` workflow leaves behind, and it
        takes precedence over anything a handler holds.

        Failing to fetch one definition's plan is treated as that definition's
        plan failure, so the same two options govern it as govern the plan
        phase: `plan_failures` halts the phase, and `fail_on_plan_error` decides
        whether the run ends non-zero. Neither applies to the apply phase
        itself, which has no equivalent failure handling yet.
        """
        options = self.app_state.terraform_options
        if not (options.apply or options.destroy):
            return
        if not (self.app_state.root_options.backend_plans or options.plan_file_path):
            return

        for name, definition in self.app_state.definitions.items():
            def_plan.set_plan_file(definition)
            needed, reason = def_plan.needs_plan(definition)

            if not needed and "plan file exists" in reason:
                definition.needs_apply = True
                continue

            if needed:
                log.info(
                    f"no saved plan for definition: {name}; it will not be applied"
                )
                definition.needs_apply = False
                continue

            try:
                retrieved = self.app_state.handlers.get_available_plan(definition)
            except HandlerError as e:
                log.error(f"handler error fetching the saved plan for {name}: {e}")
                definition.plan_failed = True
                definition.needs_apply = False
                if options.plan_failures:
                    log.warn(
                        "halting the fetch; the definitions behind "
                        f"{name} have no plan to apply"
                    )
                    break
                continue

            definition.needs_apply = retrieved
            if retrieved:
                log.info(f"definition {name} will be applied from a saved plan")
            else:
                log.warn(
                    f"saved plan for definition {name} could not be used; "
                    "it will not be applied"
                )

        if options.fail_on_plan_error:
            if any(d.plan_failed for d in self.app_state.definitions.values()):
                self.ctx.exit(1)

    def terraform_apply_or_destroy(self) -> None:
        log.trace("entering terraform apply or destroy")

        if self.app_state.terraform_options.destroy:
            action: TerraformAction = TerraformAction.DESTROY
        elif self.app_state.terraform_options.apply:
            action: TerraformAction = TerraformAction.APPLY
        else:
            # info for the same reason as the plan phase: this is the last thing
            # the run would have done
            log.info(
                "no apply or destroy requested (--no-apply and --no-destroy); "
                "no definitions will be applied"
            )
            return

        for name in self.app_state.definitions.keys():
            log.trace(f"running {action} for definition: {name}")
            if self._init_incomplete(name):
                log.warn(
                    f"skipping {action} for definition: {name}; it was not initialized"
                )
                continue
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

    def _init_incomplete(self, name: str) -> bool:
        """
        Return True when the definition was not successfully initialized.

        Covers both a definition that failed to prepare/init and one the init
        phase never reached after halting on an earlier failure.
        """
        definition: Definition = self.app_state.definitions[name]
        return definition.init_failed or definition.init_skipped

    def _handle_parallel_init_results(self, init_futures, show_output: bool) -> None:
        """
        Handle results from parallel terraform init execution.

        Failures are recorded against their definition rather than terminating
        the run here, so every definition's outcome is reported.

        Args:
            init_futures: List of (name, future) tuples from parallel execution
            show_output: Whether to show successful terraform output (based on original stream_output setting)
        """
        for name, future in init_futures:
            try:
                result = future.result()
            except click.exceptions.Exit:
                # a handler or hook failure already chose the exit code
                raise
            except Exception as e:
                self._record_init_failure(name, str(e))
                continue

            if result is not None and result.exit_code:
                # _exec_terraform_action logged the output and dispatched the
                # ERROR stage with the real result; a hook failure has already
                # recorded a more specific message
                if not self.app_state.definitions[name].init_failed:
                    self._record_init_failure(
                        name,
                        f"terraform init exited {result.exit_code}",
                        dispatch_error_stage=False,
                    )
                continue

            log.debug(f"Completed terraform init for definition: {name}")

            # Log successful output only if original stream_output was enabled
            if show_output and result and not log.json_logging_enabled():
                self._log_terraform_result(name, result)

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

    def _record_init_failure(
        self, name: str, message: str, dispatch_error_stage: bool = True
    ) -> None:
        """
        Record that a definition could not be prepared or initialized.

        The definition is flagged so the plan/apply phases skip it, the message
        is kept for the end of phase report, and the ERROR stage is dispatched
        so handlers see a failed init instead of one that never ran. When
        terraform itself failed the ERROR stage was already dispatched with the
        real result, so `dispatch_error_stage` suppresses a second dispatch.

        Args:
            name: the definition that failed
            message: why it failed
            dispatch_error_stage: dispatch the ERROR stage for the init action
        """
        self.app_state.definitions[name].init_failed = True
        self.init_errors[name] = message
        log.error(f"error initializing definition {name}: {message}")

        if dispatch_error_stage:
            # preparation failures happen before terraform runs; synthesize a
            # result so handlers have the failure detail they report on
            self._exec_error_handlers(
                name,
                TerraformAction.INIT,
                TerraformResult(1, b"", message.encode()),
            )

    def _mark_init_skipped(self, names: List[str]) -> None:
        """
        Mark the definitions the init phase never reached.

        They are not planned or applied, and handlers report them as skipped
        rather than failed; only definitions that were actually attempted are
        counted as failures.
        """
        skipped = [
            name for name in names if not self.app_state.definitions[name].init_failed
        ]
        if not skipped:
            return

        for name in skipped:
            self.app_state.definitions[name].init_skipped = True
        log.warn(
            f"not initializing {len(skipped)} definitions after an init failure; "
            "use --no-init-failures to initialize every definition"
        )

    def _prepare_definition_guarded(self, def_prep, name: str) -> bool:
        """
        Prepare a definition, recording any failure instead of raising it.

        Returns:
            bool: True when the definition is ready for terraform init
        """
        try:
            self._prepare_definition(def_prep, name)
        except click.exceptions.Exit:
            # a handler or hook failure already chose the exit code
            raise
        except Exception as e:
            self._record_init_failure(name, str(e))
            return False

        log.debug(f"Completed preparation for definition: {name}")
        return True

    def _init_definition_guarded(self, name: str) -> bool:
        """
        Run terraform init for a definition, recording any failure.

        Returns:
            bool: True when terraform init completed cleanly
        """
        try:
            result = self._terraform_init_single(name)
        except click.exceptions.Exit:
            raise
        except Exception as e:
            self._record_init_failure(name, str(e))
            return False

        if result is not None and result.exit_code:
            if not self.app_state.definitions[name].init_failed:
                self._record_init_failure(
                    name,
                    f"terraform init exited {result.exit_code}",
                    dispatch_error_stage=False,
                )
            return False
        return True

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
            raise TFWorkerException(f"preparation failed: {e}") from e

    def _terraform_init_single(self, name: str) -> "TerraformResult":
        """
        Run terraform init for a single definition

        The failing result is returned rather than exiting, the init phase
        decides whether a failure halts the run.
        """
        log.trace(f"running terraform init for definition: {name}")
        return self._exec_terraform_action(
            name=name, action=TerraformAction.INIT, exit_on_error=False
        )

    def _exec_terraform_action(
        self, name: str, action: TerraformAction, exit_on_error: bool = True
    ) -> "TerraformResult":
        """
        Execute terraform action

        Args:
            name: the definition to run the action for
            action: the terraform action to run
            exit_on_error: exit the run when terraform fails; when False the
                failing result is returned to the caller after the ERROR stage
                has been dispatched
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
        if not self._exec_hook(definition, action, TerraformStage.PRE):
            return self._hook_failure(name, action, TerraformStage.PRE, exit_on_error)

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
            self._exec_error_handlers(name, action, result)
            if exit_on_error:
                self.ctx.exit(1)
            return result

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
        if not self._exec_hook(definition, action, TerraformStage.POST, result):
            return self._hook_failure(name, action, TerraformStage.POST, exit_on_error)

        if action == TerraformAction.APPLY:
            if definition.plan_file is not None:
                Path(definition.plan_file).unlink(missing_ok=True)

        return result

    def _exec_error_handlers(
        self,
        name: str,
        action: TerraformAction,
        result: "TerraformResult",
    ) -> None:
        """
        Dispatch the ERROR stage to handlers when a terraform action fails.

        This runs in place of the POST stage (which is skipped on failure) so
        handlers can react to the failure with the failing result. Any error
        raised by a handler here is logged and swallowed so it cannot mask the
        original terraform failure. Hooks are intentionally not invoked.
        """
        try:
            log.trace(
                f"executing {TerraformStage.ERROR.value} {action.value} handlers for definition {name}"
            )
            self._app_state.handlers.exec_handlers(
                action=action,
                stage=TerraformStage.ERROR,
                deployment=self.app_state.deployment,
                definition=self.app_state.definitions[name],
                working_dir=self.app_state.working_dir,
                result=result,
            )
        except Exception as e:
            log.error(f"error-stage handler error on definition {name}: {e}")

    def _exec_terraform_pre_plan(self, name: str) -> bool:
        """
        Execute terraform pre plan with hooks and handlers for the given definition

        Returns:
            bool: False when a pre-plan hook failed, so the definition is not planned
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
        if not self._exec_hook(
            self._app_state.definitions[name],
            TerraformAction.PLAN,
            TerraformStage.PRE,
        ):
            self._hook_failure(
                name, TerraformAction.PLAN, TerraformStage.PRE, exit_on_error=False
            )
            return False
        return True

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
            definition.plan_failed = True
            # dispatch the ERROR stage so handlers see the failure; the plan
            # loop decides whether to halt and the exit code is governed by
            # fail_on_plan_error. POST success handlers/hooks are skipped.
            self._exec_error_handlers(name, TerraformAction.PLAN, result)
            return

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
        if not self._exec_hook(
            self._app_state.definitions[name],
            TerraformAction.PLAN,
            TerraformStage.POST,
            result,
        ):
            self._hook_failure(
                name, TerraformAction.PLAN, TerraformStage.POST, exit_on_error=False
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

        command = f"{self.app_state.terraform_options.terraform_bin} {terraform_command} {params}"
        # the definition and action travel as fields so a central log can be
        # filtered by them; the command line itself is detail for a debug run
        context = {
            "definition": definition_name,
            "terraform_action": action.value,
        }
        log.info(
            {
                "message": f"running terraform {terraform_command} for {definition_name}",
                **context,
            }
        )
        log.debug(
            {
                "message": f"running cmd: {command}",
                "command": f"terraform {terraform_command}",
                "working_dir": str(working_dir),
                **context,
            }
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

        result: TerraformResult = TerraformResult(
            *pipe_exec_logged(
                command,
                label=f"terraform {terraform_command}",
                cwd=working_dir,
                env=self.terraform_config.env,
                stream_output=stream_output,
                # terraform plan returns 2 when there are changes, which is a
                # successful plan, not a failure
                ok_exit_codes=((0, 2) if action == TerraformAction.PLAN else (0,)),
                extra=context,
                message=f"terraform {terraform_command} output for {definition_name}",
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
    ) -> bool:
        """
        Find and execute the appropriate hooks for a supplied definition

        A hook failure is reported to the caller rather than ending the run, so
        the phase the hook belongs to decides what happens to the definition.

        Args:
            definition (Definition): the definition to execute the hooks for
            action (TerraformAction): the action to execute the hooks for
            stage (TerraformStage): the stage to execute the hooks for
            result (TerraformResult): the result of the terraform command

        Returns:
            bool: False when a hook ran and failed, True otherwise
        """
        hook_dir = definition.get_target_path(self.app_state.working_dir)

        try:
            if not hooks.check_hooks(stage, hook_dir, action):
                log.trace(
                    f"no {stage}-{action} hooks found for definition {definition.name}"
                )
                return True

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
            return False
        return True

    def _hook_failure(
        self,
        name: str,
        action: TerraformAction,
        stage: TerraformStage,
        exit_on_error: bool,
    ) -> "TerraformResult":
        """
        Record a hook failure as a failure of the phase the hook belongs to.

        Init and plan hook failures are recorded against the definition and
        governed by that phase's options, so a single bad hook does not end a
        run; the definition itself is finished either way, no later phase acts
        on it. Apply and destroy have no such options, so a hook failure there
        remains fatal.

        Args:
            name: the definition whose hook failed
            action: the action the hook belongs to
            stage: the stage the hook belongs to
            exit_on_error: end the run rather than returning the failure

        Returns:
            TerraformResult: a failing result standing in for the phase
        """
        definition: Definition = self.app_state.definitions[name]
        message = f"{stage.value}-{action.value} hook failed"
        result = TerraformResult(2, b"", message.encode())

        if action == TerraformAction.INIT:
            # marks init_failed and dispatches the ERROR stage
            self._record_init_failure(name, message)
        elif action == TerraformAction.PLAN:
            definition.plan_failed = True
            # a plan whose hook failed must not be applied, even one that
            # already reported changes
            definition.needs_apply = False
            self._exec_error_handlers(name, action, result)
        else:
            self._exec_error_handlers(name, action, result)

        if exit_on_error:
            self.ctx.exit(2)
        return result


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
        return self.stderr.decode()

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
        """
        Environment for a terraform command, resolved per access.

        Not cached: an authenticator may hand out credentials that expire, and
        the environment a command inherits is fixed once it starts, so each
        command needs the values that are current when it launches.
        """
        return self._get_env()

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
