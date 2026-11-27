"""Report handler for terraform-worker.

Writes a durable record of each run beside the state it touched, so a
dashboard or a script can answer when a definition was last planned or
applied, how long it took, what is pending and what failed. The worker never
serves these records; it only writes them.

Two kinds of object are written under ``<prefix>/_tfworker/``:

- ``runs/YYYY/MM/DD/<record_id>.json``: one immutable ``tfworker.run/v1`` record
  per run, written when the run ends, even when it failed.
- ``status/<definition>.json``: the latest ``tfworker.status/v1`` snapshot of
  each definition, updated with a conditional write and retried when another
  run updated it first.

Records hold resource addresses, actions, counts and timings. They never hold
attribute values from plans or state.

Example configuration::

    handlers:
      report:
        resources: true        # record changed resource addresses in run records
        state_serials: true    # read state lineage and serial around plan and apply
        tracked_ref: origin/main   # default: the remote's default branch
"""

import json
import random
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Optional, Union

import click
from pydantic import BaseModel, ConfigDict, Field

import tfworker.report.events as events
import tfworker.util.log as log
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.exceptions import BackendError, ReportConflictError
from tfworker.report import (
    ChangeSummary,
    DefinitionResult,
    DefinitionStatus,
    PhaseResult,
    Provenance,
    RunRecord,
    new_ulid,
)
from tfworker.report.events import RUN_EVENT, STATUS_EVENT
from tfworker.report.models import LOCAL_MODULE_TAG, STATUS_SCHEMA
from tfworker.report.status import merge_status
from tfworker.util.provenance import current_run_id, module_sources, run_provenance
from tfworker.util.system import strip_ansi

from .base import BaseHandler
from .registry import HandlerRegistry

if TYPE_CHECKING:  # pragma: no cover
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition

# the frame terraform draws around diagnostics
_BOX = re.compile(r"^[\s│╷╵]+")
REASON_LIMIT = 300

REPORTED_ACTIONS = [
    TerraformAction.INIT,
    TerraformAction.PLAN,
    TerraformAction.APPLY,
    TerraformAction.DESTROY,
]


class ReportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resources: bool = True
    state_serials: bool = True
    tracked_ref: Optional[str] = None
    status_retries: int = Field(5, ge=1)


@HandlerRegistry.register("report")
class ReportHandler(BaseHandler):
    """Records each run and each definition's latest status beside its state."""

    actions = REPORTED_ACTIONS
    config_model = ReportConfig
    # run last at PRE and POST, so other handlers' work counts in the timings
    default_priority = {action: 1000 for action in REPORTED_ACTIONS}

    def __init__(self, config: ReportConfig):
        self.config = config
        self._app_state = None
        self._lock = threading.Lock()
        self._results: Dict[str, DefinitionResult] = {}
        self._phase_starts: Dict[tuple, datetime] = {}
        self._run_id: Optional[str] = None
        self._run_id_generated = False
        self._started_at: Optional[datetime] = None
        self._deployment: Optional[str] = None
        self._record_id = new_ulid()
        self._warned_not_ready = False
        self._now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
        self._sleep: Callable[[float], None] = time.sleep

    @property
    def app_state(self):
        if self._app_state is None:
            self._app_state = click.get_current_context().obj
        return self._app_state

    @property
    def backend(self):
        return self.app_state.backend

    @property
    def run_id(self) -> str:
        if self._run_id is None:
            self._run_id, self._run_id_generated = current_run_id(self.app_state)
        return self._run_id

    def is_ready(self) -> bool:
        backend = getattr(self.app_state, "backend", None)
        ready = bool(getattr(backend, "report_storage", False))
        if not ready and backend is not None and not self._warned_not_ready:
            log.warn("the backend cannot store reports; the report handler is off")
            self._warned_not_ready = True
        return ready

    def setup(self, deployment, definitions, working_dir, terraform_options) -> None:
        self._deployment = deployment
        self._started_at = self._now()
        log.info(f"recording this run as {self.run_id}")

    def execute(
        self,
        action: TerraformAction,
        stage: TerraformStage,
        deployment: str,
        definition: "Definition",
        working_dir: str,
        result: Union["TerraformResult", None] = None,
    ) -> None:
        # reporting must never change the outcome of a run
        try:
            self._execute(action, stage, definition, working_dir, result)
        except Exception as e:
            log.warn(
                f"unable to record {action.value} {stage.value} for "
                f"{definition.name}: {type(e).__name__}: {e}"
            )

    def _execute(
        self,
        action: TerraformAction,
        stage: TerraformStage,
        definition: "Definition",
        working_dir: str,
        result: Optional["TerraformResult"],
    ) -> None:
        self._ensure_entry(definition.name)
        if stage == TerraformStage.PRE:
            with self._lock:
                self._phase_starts[(definition.name, action)] = self._now()
            if action == TerraformAction.PLAN and self.config.state_serials:
                lineage = self._state_version(definition.name)
                with self._lock:
                    entry = self._results[definition.name]
                    entry.base_lineage, entry.base_serial = lineage or (None, None)
            return

        succeeded = stage == TerraformStage.POST
        with self._lock:
            started = (definition.name, action) in self._phase_starts
            recorded = self._results[definition.name].phases.get(action.value)
        if (
            not succeeded
            and not started
            and recorded
            and recorded.status == "succeeded"
        ):
            # a POST hook failed after terraform finished: keep the real timings
            with self._lock:
                recorded.status = "failed"
                recorded.reason = self._failure_reason(result) or (
                    f"post-{action.value} hook failed"
                )
            return

        phase = self._phase(action, definition, result, succeeded)
        with self._lock:
            self._results[definition.name].phases[action.value] = phase

        if action == TerraformAction.PLAN and succeeded:
            changes = self._plan_changes(definition)
            with self._lock:
                self._results[definition.name].changes = changes
        if action == TerraformAction.INIT and succeeded:
            sources = module_sources(definition.get_target_path(working_dir))
            with self._lock:
                self._results[definition.name].module_sources = sources
        if action in (TerraformAction.APPLY, TerraformAction.DESTROY):
            applied = None
            if self.config.state_serials:
                version = self._state_version(definition.name)
                applied = version[1] if version else None
            with self._lock:
                entry = self._results[definition.name]
                entry.applied_serial = applied
                if succeeded and any(m.kind == "local" for m in entry.module_sources):
                    if LOCAL_MODULE_TAG not in entry.tags:
                        entry.tags.append(LOCAL_MODULE_TAG)

    def skip(self, action, deployment, definition, reason) -> None:
        self._ensure_entry(definition.name)
        with self._lock:
            entry = self._results[definition.name]
            if action.value not in entry.phases:
                entry.phases[action.value] = PhaseResult(
                    action=action.value, status="skipped", reason=reason
                )

    def teardown(self, deployment: str, working_dir: str) -> None:
        with self._lock:
            # a phase that started but never finished: the run stopped under it
            for (name, action), started in self._phase_starts.items():
                entry = self._results.get(name)
                if entry is not None:
                    entry.phases[action.value] = PhaseResult(
                        action=action.value,
                        status="failed",
                        started_at=started,
                        reason="the run stopped before this finished",
                    )
            self._phase_starts.clear()
            results = [self._results[name] for name in sorted(self._results)]
        if not results:
            log.debug("no definitions ran; no run record written")
            return
        record = self._record(results)
        self._write_record(record)
        for result in results:
            self._write_status(record, result)

    def _ensure_entry(self, name: str) -> None:
        with self._lock:
            if name in self._results:
                return
        # the location may call STS; keep that outside the lock
        location = self.backend.location(name)
        with self._lock:
            self._results.setdefault(
                name, DefinitionResult(definition=name, state=location)
            )

    def _phase(
        self,
        action: TerraformAction,
        definition: "Definition",
        result: Optional["TerraformResult"],
        succeeded: bool,
    ) -> PhaseResult:
        with self._lock:
            started = self._phase_starts.pop((definition.name, action), None)
        finished = getattr(result, "finished_at", None) or self._now()
        started = getattr(result, "started_at", None) or started
        duration = getattr(result, "duration", None)
        if duration is None and started is not None:
            duration = (finished - started).total_seconds()
        wait = definition.lock_wait_seconds
        definition.lock_wait_seconds = None
        return PhaseResult(
            action=action.value,
            status="succeeded" if succeeded else "failed",
            started_at=started,
            finished_at=finished,
            duration_seconds=duration,
            exit_code=getattr(result, "exit_code", None),
            reason=None if succeeded else self._failure_reason(result),
            lock_wait_seconds=wait,
        )

    @staticmethod
    def _failure_reason(result: Optional["TerraformResult"]) -> Optional[str]:
        """
        The summary line of terraform's first error, such as
        "Error: creating S3 Bucket: AccessDenied". The detail lines below it
        are left out, since provider messages there can carry values.
        """
        stderr = getattr(result, "stderr", None)
        if not stderr:
            return None
        lines = [
            _BOX.sub("", strip_ansi(line)).strip()
            for line in stderr.decode(errors="replace").splitlines()
        ]
        lines = [line for line in lines if line]
        errors = [line for line in lines if line.startswith("Error:")]
        first = (errors or lines or [None])[0]
        return first[:REASON_LIMIT] if first else None

    def _plan_changes(self, definition: "Definition") -> Optional[ChangeSummary]:
        """The plan's changes; None when they could not be read."""
        if not definition.needs_apply or not definition.plan_file:
            return ChangeSummary()
        jsonfile = Path(definition.plan_file).with_suffix(".tfplan.json")
        try:
            summary = ChangeSummary.from_plan_json(json.loads(jsonfile.read_text()))
        except (OSError, ValueError) as e:
            log.warn(f"unable to read plan changes for {definition.name}: {e}")
            return None
        return summary if self.config.resources else summary.counts_only()

    def _state_version(self, name: str):
        try:
            return self.backend.state_version(name)
        except BackendError as e:
            log.warn(f"unable to read the state serial of {name}: {e}")
            return None

    def _provenance(self) -> Provenance:
        return run_provenance(self.app_state, self.config.tracked_ref)

    def _record(self, results) -> RunRecord:
        tf = self.app_state.terraform_options
        actions = [
            name
            for name, on in (
                ("plan", getattr(tf, "plan", False)),
                ("apply", getattr(tf, "apply", False)),
                ("destroy", getattr(tf, "destroy", False)),
            )
            if on
        ]
        failed = any(result.failed for result in results)
        return RunRecord(
            record_id=self._record_id,
            run_id=self.run_id,
            run_id_generated=self._run_id_generated,
            deployment=self._deployment or self.app_state.deployment,
            started_at=self._started_at or self._now(),
            finished_at=self._now(),
            status="failed" if failed else "succeeded",
            actions=actions,
            provenance=self._provenance(),
            definitions=results,
        )

    def _write_record(self, record: RunRecord) -> None:
        # keyed by this invocation, since a plan job and its apply job can
        # share one --run-id
        day = record.started_at.strftime("%Y/%m/%d")
        path = f"runs/{day}/{record.record_id}.json"
        try:
            self.backend.write_report(
                path, record.model_dump_json(by_alias=True), create_only=True
            )
            log.info(f"run record written: {path}")
        except ReportConflictError:
            log.warn(f"run record {path} already exists; not replaced")
            return
        except BackendError as e:
            log.warn(f"unable to write the run record {path}: {e}")
            return
        events.publish(RUN_EVENT, record.deployment, None, record, record.run_id)

    def _write_status(self, record: RunRecord, result: DefinitionResult) -> None:
        path = f"status/{result.definition}.json"
        for attempt in range(self.config.status_retries):
            if attempt:
                self._sleep(random.uniform(0.05, 0.25) * 2**attempt)
            try:
                found = self.backend.read_report(path)
                current, etag = None, None
                if found is not None:
                    text, etag = found
                    schema = _schema_of(text)
                    if schema is not None and schema != STATUS_SCHEMA:
                        log.warn(
                            f"status {path} is {schema}, which this worker cannot "
                            "update; leaving it as is"
                        )
                        return
                    try:
                        current = DefinitionStatus.model_validate_json(text)
                    except ValueError as e:
                        log.warn(f"replacing unreadable status {path}: {e}")
                status = merge_status(current, record, result, self._now())
                self.backend.write_report(
                    path,
                    status.model_dump_json(by_alias=True),
                    if_match=etag,
                    create_only=found is None,
                )
                events.publish(
                    STATUS_EVENT,
                    status.deployment,
                    status.definition,
                    status,
                    record.run_id,
                )
                return
            except ReportConflictError:
                log.debug(f"status {path} changed while updating; retrying")
            except BackendError as e:
                log.warn(f"unable to update the status {path}: {e}")
                return
        log.warn(
            f"gave up updating the status {path} after "
            f"{self.config.status_retries} conflicting writes"
        )


def _schema_of(text: str) -> Optional[str]:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data.get("schema") if isinstance(data, dict) else None
