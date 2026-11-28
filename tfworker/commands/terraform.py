import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from shlex import quote as shlex_quote
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple, Union

import click

import tfworker.util.hooks as hooks
import tfworker.util.log as log
import tfworker.util.terraform as tf_util
from tfworker.commands.base import BaseCommand
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.definitions import Definition
from tfworker.exceptions import (
    DefinitionRefusedError,
    HandlerError,
    HookError,
    LockUnavailableError,
    TFWorkerException,
)
from tfworker.locking import StateLocker
from tfworker.phases import PhaseDispatcher
from tfworker.reporting import RunRecorder
from tfworker.util.duration import parse_duration
from tfworker.util.provenance import current_run_id
from tfworker.util.system import pipe_exec, pipe_exec_logged
from tfworker.util.terraform import quote_index_brackets

if TYPE_CHECKING:
    from tfworker.app_state import AppState
    from tfworker.definitions.plan import DefinitionPlan


# terraform prints this when it gives up waiting for a held state lock
LOCK_ERROR_MARKER = b"Error acquiring the state lock"


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

    # set when an always_apply failure in the plan phase halts applies
    _apply_halted: bool = False

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

    @property
    def apply_errors(self) -> Dict[str, str]:
        """Failure message per definition whose apply or destroy failed."""
        if not hasattr(self, "_apply_errors"):
            self._apply_errors: Dict[str, str] = {}
        return self._apply_errors

    @property
    def skips_notified(self) -> Set[Tuple[str, TerraformAction]]:
        """The (definition, action) pairs handlers were told were skipped."""
        if not hasattr(self, "_skips_notified"):
            self._skips_notified: Set[Tuple[str, TerraformAction]] = set()
        return self._skips_notified

    @property
    def lock_waits(self) -> Dict[str, float]:
        """Seconds each definition's running phase waited for its state lock."""
        if not hasattr(self, "_lock_waits"):
            self._lock_waits: Dict[str, float] = {}
        return self._lock_waits

    @property
    def phases(self) -> PhaseDispatcher:
        """The core components told about each phase, such as run reporting."""
        if not hasattr(self, "_phases"):
            recorder = RunRecorder.from_app_state(self.app_state)
            self._phases = PhaseDispatcher([recorder] if recorder else [])
        return self._phases

    def _dispatch_phase(
        self,
        name: str,
        action: TerraformAction,
        stage: TerraformStage,
        result: Optional["TerraformResult"] = None,
    ) -> None:
        """Tell core components that a phase of a definition started, finished or failed."""
        lock_wait = None
        if stage != TerraformStage.PRE:
            lock_wait = self.lock_waits.pop(name, None)
        self.phases.phase(
            action,
            stage,
            self.app_state.definitions[name],
            self.app_state.working_dir,
            result=result,
            lock_wait=lock_wait,
        )

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
                str(self.app_state.working_dir),
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

        names = list(self.app_state.definitions.keys())
        for index, name in enumerate(names):
            if self._init_incomplete(name):
                log.warn(
                    f"skipping plan for definition: {name}; it was not initialized"
                )
                self._notify_skip(name, TerraformAction.PLAN, "not initialized")
                continue
            log.info(f"running pre-plan for definition: {name}")
            if not self._exec_terraform_pre_plan(name=name):
                if self.app_state.terraform_options.plan_failures:
                    self._notify_halted(names[index + 1 :], TerraformAction.PLAN)
                    break
                continue
            needed, reason = def_plan.needs_plan(self.app_state.definitions[name])
            if not needed:
                log.info(f"Plan not needed for definition: {name}, reason: {reason}")
                self._notify_skip(name, TerraformAction.PLAN, reason)
                continue
            log.info(f"definition {name} needs a plan: {reason}")
            self._exec_terraform_plan(name=name)
            if (
                self.app_state.definitions[name].plan_failed
                and self.app_state.terraform_options.plan_failures
            ):
                self._notify_halted(names[index + 1 :], TerraformAction.PLAN)
                break
            if not self.app_state.definitions[name].plan_failed and getattr(
                self.app_state.definitions[name], "always_apply", False
            ):
                self._always_apply(name)

        if self.app_state.terraform_options.fail_on_plan_error:
            if any(d.plan_failed for d in self.app_state.definitions.values()):
                if self.apply_errors:
                    self._report_apply_results(TerraformAction.APPLY)
                self.ctx.exit(1)

    def _always_apply(self, name: str) -> None:
        """
        Apply an always_apply definition right after its plan.

        Once an apply has failed and `apply_failures` is set, later
        always_apply definitions keep their plan and are marked skipped, and
        the apply phase skips every remaining definition.
        """
        definition = self.app_state.definitions[name]
        if self._apply_halted:
            if definition.needs_apply:
                self._mark_apply_skipped([name], TerraformAction.APPLY)
            return

        log.info(
            f"definition {name} has always_apply set; applying immediately after plan"
        )
        applied = self._apply_definition(name, TerraformAction.APPLY)
        definition.needs_apply = False
        if not applied and self.app_state.terraform_options.apply_failures:
            log.warn(
                f"halting applies after the always_apply failure of {name}; "
                "planning continues"
            )
            self._apply_halted = True

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
        whether the run ends non-zero.
        """
        options = self.app_state.terraform_options
        if not (options.apply or options.destroy):
            return
        if not (self.app_state.root_options.backend_plans or options.plan_file_path):
            return
        action = TerraformAction.DESTROY if options.destroy else TerraformAction.APPLY

        names = list(self.app_state.definitions.keys())
        for index, name in enumerate(names):
            definition = self.app_state.definitions[name]
            if self._init_incomplete(name):
                definition.needs_apply = False
                self._notify_skip(name, action, "not initialized")
                continue
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
                self._notify_skip(name, action, "no saved plan")
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
                    # a definition with a plan already on disk is still applied
                    self._notify_halted(
                        [
                            n
                            for n in names[index + 1 :]
                            if not self.app_state.definitions[n].needs_apply
                        ],
                        action,
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
                self._notify_skip(name, action, "saved plan could not be used")

        if options.fail_on_plan_error:
            if any(d.plan_failed for d in self.app_state.definitions.values()):
                self.ctx.exit(1)

    def terraform_apply_or_destroy(self) -> None:
        """
        Apply or destroy every definition that has a plan to act on.

        Two options govern the phase, mirroring the init and plan options:

        - `apply_failures`: halt the phase when a definition fails, leaving the
          remaining definitions unapplied (default)
        - `fail_on_apply_error`: exit non-zero once the phase completes if any
          definition failed (default)

        A failure of terraform itself or of a PRE or POST hook counts as the
        definition's failure. Each failed definition is marked `apply_failed`,
        and each definition left behind by a halt is marked `apply_skipped`.
        """
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
            # always_apply definitions are applied by the plan phase
            self._report_apply_results(TerraformAction.APPLY)
            return

        candidates = self._apply_candidates(action)

        if self._apply_halted:
            self._mark_apply_skipped(candidates, action)
        else:
            for index, name in enumerate(candidates):
                log.info(f"running {action} for definition: {name}")
                if self._apply_definition(name, action):
                    continue
                if self.app_state.terraform_options.apply_failures:
                    self._mark_apply_skipped(candidates[index + 1 :], action)
                    break

        self._report_apply_results(action)

    def _apply_candidates(self, action: TerraformAction) -> List[str]:
        """
        Return the definitions the apply or destroy phase will act on, in order.

        A definition qualifies when it initialized, has a plan to apply whose
        file exists, and, for a destroy, is inside `--limit` when one is set.
        """
        candidates: List[str] = []
        limit = self.app_state.terraform_options.limit
        for name, definition in self.app_state.definitions.items():
            log.trace(f"checking {action} for definition: {name}")
            if self._init_incomplete(name):
                log.warn(
                    f"skipping {action} for definition: {name}; it was not initialized"
                )
                self._notify_skip(name, action, "not initialized")
                continue
            if action == TerraformAction.DESTROY and limit and name not in limit:
                log.info(f"skipping destroy for definition: {name}")
                self._notify_skip(name, action, "outside --limit")
                continue
            log.trace(f"definition {name} needs_apply value: {definition.needs_apply}")
            if not definition.needs_apply:
                continue
            plan_file = definition.plan_file
            if plan_file is None or not Path(plan_file).exists():
                log.info(
                    f"plan file does not exist for definition: {name}; skipping {action}"
                )
                self._notify_skip(name, action, "plan file does not exist")
                continue
            candidates.append(name)
        return candidates

    def _apply_definition(self, name: str, action: TerraformAction) -> bool:
        """
        Apply or destroy one definition, recording a failure on it.

        Returns:
            bool: True when terraform and the definition's hooks succeeded
        """
        result = self._exec_terraform_action(
            name=name, action=action, exit_on_error=False
        )
        return not result.exit_code

    def _record_apply_failure(self, name: str, message: str) -> None:
        """
        Record that a definition failed to apply or destroy.

        Called before the ERROR stage is dispatched, so ERROR handlers see
        `apply_failed` set.
        """
        self.app_state.definitions[name].apply_failed = True
        self.apply_errors[name] = message
        log.error(f"error applying definition {name}: {message}")

    def _notify_skip(self, name: str, action: TerraformAction, reason: str) -> None:
        """
        Tell handlers that a phase passed over a definition without running terraform.

        Each definition is reported at most once per action: the apply phase
        can reach a definition the plan phase already passed over.
        """
        if (name, action) in self.skips_notified:
            return
        self.skips_notified.add((name, action))
        self.phases.skip(action, self.app_state.definitions[name], reason)
        self.app_state.handlers.exec_skip(
            action=action,
            deployment=self.app_state.deployment,
            definition=self.app_state.definitions[name],
            reason=reason,
        )

    def _notify_halted(self, names: List[str], action: TerraformAction) -> None:
        """Tell handlers about the definitions an earlier failure kept the phase from reaching."""
        for name in names:
            self._notify_skip(
                name, action, f"{action.value} halted by an earlier failure"
            )

    def _mark_apply_skipped(self, names: List[str], action: TerraformAction) -> None:
        """Mark the definitions a halted apply or destroy phase never reached."""
        if not names:
            return
        for name in names:
            self.app_state.definitions[name].apply_skipped = True
        self._notify_halted(names, action)
        log.warn(
            f"not running {action} for {len(names)} definitions after a failure; "
            "use --no-apply-failures to attempt every definition"
        )

    def _report_apply_results(self, action: TerraformAction) -> None:
        """
        Report the outcome of the apply or destroy phase, and exit if it is fatal.

        Every failure of the phase is reported together here; the exit code is
        governed by `fail_on_apply_error`.
        """
        definitions = self.app_state.definitions
        failed = [name for name in definitions.keys() if definitions[name].apply_failed]
        skipped = [
            name for name in definitions.keys() if definitions[name].apply_skipped
        ]

        if not failed:
            return

        log.error(f"{len(failed)} definitions failed to {action.value}:")
        for name in failed:
            log.error(f"  {name}: {self.apply_errors.get(name, 'unknown error')}")
        if skipped:
            log.warn(
                f"{len(skipped)} definitions were not attempted: {', '.join(skipped)}"
            )

        if self.app_state.terraform_options.fail_on_apply_error:
            self.ctx.exit(1)

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
                TerraformResult.synthesized(1, message),
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
        self._notify_halted(skipped, TerraformAction.INIT)
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
        except DefinitionRefusedError as e:
            return self._definition_refused(name, action, e, exit_on_error)
        except HandlerError as e:
            log.error(f"handler error on definition {name}: {e}")
            self.ctx.exit(2)
        self._dispatch_phase(name, action, TerraformStage.PRE)

        log.trace(
            f"executing {TerraformStage.PRE} {action.value} hooks for definition {name}"
        )
        if not self._exec_hook(definition, action, TerraformStage.PRE):
            return self._hook_failure(name, action, TerraformStage.PRE, exit_on_error)

        log.trace(f"running terraform {action.value} for definition {name}")
        try:
            result = self._run_locked(name, action)
        except LockUnavailableError as e:
            return self._definition_refused(name, action, e, exit_on_error)
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
            fail_run = self._lock_held_failure(name, result)
            if action in (TerraformAction.APPLY, TerraformAction.DESTROY):
                self._record_apply_failure(
                    name, f"terraform {action.value} exited {result.exit_code}"
                )
            self._exec_error_handlers(name, action, result)
            if exit_on_error or fail_run:
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
        self._dispatch_phase(name, action, TerraformStage.POST, result)

        if action == TerraformAction.APPLY:
            if definition.plan_file is not None:
                Path(definition.plan_file).unlink(missing_ok=True)

        log.trace(
            f"executing {TerraformStage.POST.value} {action.value} hooks for definition {name}"
        )
        if not self._exec_hook(definition, action, TerraformStage.POST, result):
            return self._hook_failure(name, action, TerraformStage.POST, exit_on_error)

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
        self._dispatch_phase(name, action, TerraformStage.ERROR, result)

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
        except DefinitionRefusedError as e:
            self._definition_refused(name, TerraformAction.PLAN, e, exit_on_error=False)
            return False
        except HandlerError as e:
            log.error(f"handler error on definition {name}: {e}")
            self.ctx.exit(2)
        self._dispatch_phase(name, TerraformAction.PLAN, TerraformStage.PRE)

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

        try:
            result = self._run_locked(name, TerraformAction.PLAN)
        except LockUnavailableError as e:
            self._definition_refused(name, TerraformAction.PLAN, e, exit_on_error=False)
            return

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
            fail_run = self._lock_held_failure(name, result)
            # dispatch the ERROR stage so handlers see the failure; the plan
            # loop decides whether to halt and the exit code is governed by
            # fail_on_plan_error. POST success handlers/hooks are skipped.
            self._exec_error_handlers(name, TerraformAction.PLAN, result)
            if fail_run:
                self.ctx.exit(1)
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
        self._dispatch_phase(name, TerraformAction.PLAN, TerraformStage.POST, result)

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

        working_dir: str = definition.get_target_path(self.app_state.working_dir)

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

    @property
    def state_locker(self) -> Optional[StateLocker]:
        """The state locker when `locking` is configured, else None."""
        if not hasattr(self, "_state_locker"):
            options = getattr(self.app_state.loaded_config, "locking", None)
            self._state_locker = None
            if options is not None:
                if getattr(self.app_state.terraform_options, "lock_timeout", None):
                    log.info(
                        "--lock-timeout is ignored because locking is configured; "
                        "terraform gets what is left of its wait instead"
                    )
                self._state_locker = StateLocker(
                    options,
                    self.app_state.backend,
                    self.app_state.terraform_options.terraform_bin,
                    env=lambda: terraform_env(self.app_state),
                    run_id=current_run_id(self.app_state)[0],
                    deployment=self.app_state.deployment,
                )
        return self._state_locker

    def _run_locked(self, name: str, action: TerraformAction) -> "TerraformResult":
        """
        Run terraform once the definition's state lock is free, keeping the
        worker lease while it runs.

        Raises:
            LockUnavailableError: the state lock could not be acquired
        """
        locker = self.state_locker
        if locker is None or not locker.covers(action):
            return self._run(name, action)
        definition: Definition = self.app_state.definitions[name]
        started = time.monotonic()
        try:
            deadline = locker.acquire(
                action, name, definition.get_target_path(self.app_state.working_dir)
            )
        finally:
            self.lock_waits[name] = time.monotonic() - started
        try:
            return self._run(name, action, lock_deadline=deadline)
        finally:
            locker.release(name)

    def _lock_timeout(self, lock_deadline: Optional[float]) -> Optional[float]:
        """
        Seconds terraform may wait for the state lock, or None for terraform's default.

        With locking configured, whatever is left of its wait applies;
        otherwise `--lock-timeout` does.
        """
        if lock_deadline is not None:
            return max(lock_deadline - time.monotonic(), 0.0)
        option = getattr(self.app_state.terraform_options, "lock_timeout", None)
        if option is None:
            return None
        return parse_duration(option)

    def _run(
        self,
        definition_name: str,
        action: TerraformAction,
        lock_deadline: Optional[float] = None,
    ) -> "TerraformResult":
        """
        run terraform
        """
        definition: Definition = self.app_state.definitions[definition_name]
        stream_output: bool = self.terraform_config.stream_output
        params: str = self.terraform_config.get_params(
            action,
            plan_file=definition.plan_file,
            lock_timeout=self._lock_timeout(lock_deadline),
        )

        working_dir: str = definition.get_target_path(self.app_state.working_dir)

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

        started_at = datetime.now(timezone.utc)
        started = time.monotonic()
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
        result.started_at = started_at
        result.finished_at = datetime.now(timezone.utc)
        result.elapsed = time.monotonic() - started

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

    def _lock_held_failure(self, name: str, result: "TerraformResult") -> bool:
        """
        Log a terraform failure caused by a state lock that was still held when
        -lock-timeout ran out, and return whether it should stop the run.
        """
        output = (result.stderr or b"") + (result.stdout or b"")
        if LOCK_ERROR_MARKER not in output:
            return False
        log.error(
            f"state lock unavailable for definition {name}: terraform could not "
            "acquire the lock"
        )
        return self.state_locker is not None and self.state_locker.fail_run

    def _definition_refused(
        self,
        name: str,
        action: TerraformAction,
        error: DefinitionRefusedError,
        exit_on_error: bool,
    ) -> "TerraformResult":
        """
        Record that terraform could not run for a definition: its state lock
        stayed held, or a handler refused it, such as a saved plan that failed
        the apply gate.

        It counts as a failure of that definition's plan, apply or destroy and
        follows the phase's failure options, like a failing hook, unless the
        error asks for the whole run to stop.
        """
        definition: Definition = self.app_state.definitions[name]
        log.error(f"{error.kind} for definition {name}: {error.message}")
        result = TerraformResult.synthesized(1, error.message)

        if action == TerraformAction.PLAN:
            definition.plan_failed = True
            definition.needs_apply = False
        else:
            self._record_apply_failure(name, error.message)
        self._exec_error_handlers(name, action, result)

        if exit_on_error or error.fail_run:
            self.ctx.exit(1)
        return result

    def _hook_failure(
        self,
        name: str,
        action: TerraformAction,
        stage: TerraformStage,
        exit_on_error: bool,
    ) -> "TerraformResult":
        """
        Record a hook failure as a failure of the phase the hook belongs to.

        The failure is recorded against the definition and governed by that
        phase's options; the definition itself is finished either way, no
        later phase acts on it.

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
        result = TerraformResult.synthesized(2, message)

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
            self._record_apply_failure(name, message)
            self._exec_error_handlers(name, action, result)

        if exit_on_error:
            self.ctx.exit(2)
        return result


class TerraformResult:
    """
    Hold the results of a terraform run
    """

    def __init__(
        self,
        exit_code: int,
        stdout: bytes,
        stderr: bytes,
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None,
    ):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.started_at = started_at
        self.finished_at = finished_at
        # monotonic seconds, immune to wall clock steps; set by _run
        self.elapsed: Optional[float] = None
        # True when terraform never ran, such as a failed hook or a refusal
        self.outside_terraform = False

    @classmethod
    def synthesized(cls, exit_code: int, message: str) -> "TerraformResult":
        """
        A result standing in for a failure that happened outside terraform,
        such as a failed hook. Its start and finish are the moment of failure.
        """
        now = datetime.now(timezone.utc)
        result = cls(exit_code, b"", message.encode(), started_at=now, finished_at=now)
        result.outside_terraform = True
        return result

    @property
    def duration(self) -> Optional[float]:
        """Seconds terraform ran for, or None when the run was not timed."""
        if self.elapsed is not None:
            return self.elapsed
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

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

    def get_params(
        self,
        command: TerraformAction,
        plan_file: str,
        lock_timeout: Optional[float] = None,
    ) -> str:
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

        lock_args = ""
        if lock_timeout is not None:
            lock_args = f" -lock-timeout={math.ceil(lock_timeout)}s"

        return {
            TerraformAction.INIT: f"-input=false {color_str} {read_only} -plugin-dir={self._app_state.terraform_options.provider_cache}",
            TerraformAction.PLAN: f"-input=false {color_str} {plan_action} -detailed-exitcode{lock_args} -out {plan_file}{target_args}",
            TerraformAction.APPLY: f"-input=false {color_str}{lock_args} -auto-approve {plan_file}",
            TerraformAction.DESTROY: f"-input=false {color_str}{lock_args} -auto-approve {plan_file}",
        }[command]

    def _get_env(self) -> Dict[str, str]:
        return terraform_env(self._app_state)


def terraform_env(app_state: "AppState") -> Dict[str, str]:
    """
    Build the environment every terraform command runs with.

    Shared by TerraformCommandConfig and definition preparation
    (``terraform get``), so all terraform invocations see the same
    authenticator credentials.

    Args:
        app_state (AppState): The application state.

    Returns:
        Dict[str, str]: The environment for a terraform subprocess.
    """
    env = os.environ.copy()
    # acknowledge that we are using a plugin cache; and compute the lockfile each run
    env["TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE"] = "1"
    # reduce non essential terraform output
    env["TF_IN_AUTOMATION"] = "1"

    # authenticators is optional on AppState; treat a missing collection as empty
    for auth in app_state.authenticators or []:
        env.update(auth.env())
    return env
