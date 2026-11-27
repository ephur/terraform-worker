"""Report handler for terraform-worker.

Writes a durable record of each run beside the state it touched, so a
dashboard or a script can answer when a definition was last planned or
applied, how long it took, what is pending and what failed. The worker never
serves these records; it only writes them.

Two kinds of object are written under ``<prefix>/_tfworker/``:

- ``runs/YYYY/MM/DD/<run_id>.json``: one immutable ``tfworker.run/v1`` record
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

import getpass
import json
import socket
import threading
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Optional, Union

import click
from pydantic import BaseModel, ConfigDict, Field

import tfworker.util.log as log
from tfworker.cli_options import parse_labels
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
from tfworker.report.models import LOCAL_MODULE_TAG
from tfworker.report.status import merge_status
from tfworker.util.provenance import module_sources, repository_state

from .base import BaseHandler
from .registry import HandlerRegistry

if TYPE_CHECKING:  # pragma: no cover
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition

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
        self._now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

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
            given = getattr(self.app_state.root_options, "run_id", None)
            self._run_id_generated = not given
            self._run_id = given or new_ulid()
        return self._run_id

    def is_ready(self) -> bool:
        backend = getattr(self.app_state, "backend", None)
        return bool(getattr(backend, "report_storage", False))

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
        if stage == TerraformStage.PRE:
            with self._lock:
                self._phase_starts[(definition.name, action)] = self._now()
            if action == TerraformAction.PLAN and self.config.state_serials:
                lineage = self._state_version(definition.name)
                with self._lock:
                    entry = self._entry(definition.name)
                    entry.base_lineage, entry.base_serial = lineage or (None, None)
            return

        succeeded = stage == TerraformStage.POST
        phase = self._phase(action, definition, result, succeeded)
        changes = None
        if action == TerraformAction.PLAN and succeeded:
            changes = self._plan_changes(definition)
        sources = None
        if action == TerraformAction.INIT and succeeded:
            sources = module_sources(definition.get_target_path(working_dir))
        applied = None
        if (
            action in (TerraformAction.APPLY, TerraformAction.DESTROY)
            and self.config.state_serials
        ):
            version = self._state_version(definition.name)
            applied = version[1] if version else None

        with self._lock:
            entry = self._entry(definition.name)
            entry.phases[action.value] = phase
            if changes is not None:
                entry.changes = changes
            if sources is not None:
                entry.module_sources = sources
            if action in (TerraformAction.APPLY, TerraformAction.DESTROY):
                entry.applied_serial = applied
                if succeeded and any(m.kind == "local" for m in entry.module_sources):
                    if LOCAL_MODULE_TAG not in entry.tags:
                        entry.tags.append(LOCAL_MODULE_TAG)

    def skip(self, action, deployment, definition, reason) -> None:
        with self._lock:
            entry = self._entry(definition.name)
            if action.value not in entry.phases:
                entry.phases[action.value] = PhaseResult(
                    action=action.value, status="skipped", reason=reason
                )

    def teardown(self, deployment: str, working_dir: str) -> None:
        with self._lock:
            results = [self._results[name] for name in sorted(self._results)]
        if not results:
            log.debug("no definitions ran; no run record written")
            return
        record = self._record(results)
        self._write_record(record)
        for result in results:
            self._write_status(record, result)

    def _entry(self, name: str) -> DefinitionResult:
        entry = self._results.get(name)
        if entry is None:
            entry = DefinitionResult(definition=name, state=self.backend.location(name))
            self._results[name] = entry
        return entry

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
        stderr = getattr(result, "stderr", None)
        if not stderr:
            return None
        lines = [line.strip() for line in stderr.decode(errors="replace").splitlines()]
        errors = [line for line in lines if line.startswith("Error:")]
        first = (errors or [line for line in lines if line] or [None])[0]
        return first[:500] if first else None

    def _plan_changes(self, definition: "Definition") -> ChangeSummary:
        if not definition.needs_apply or not definition.plan_file:
            return ChangeSummary()
        jsonfile = Path(definition.plan_file).with_suffix(".tfplan.json")
        try:
            summary = ChangeSummary.from_plan_json(json.loads(jsonfile.read_text()))
        except (OSError, ValueError) as e:
            log.warn(f"unable to read plan changes for {definition.name}: {e}")
            return ChangeSummary()
        return summary if self.config.resources else summary.counts_only()

    def _state_version(self, name: str):
        try:
            return self.backend.state_version(name)
        except BackendError as e:
            log.warn(f"unable to read the state serial of {name}: {e}")
            return None

    def _provenance(self) -> Provenance:
        root = self.app_state.root_options
        tf = self.app_state.terraform_options
        if getattr(root, "backend_plans", False):
            storage = "backend"
        elif getattr(tf, "plan_file_path", None):
            storage = "local"
        else:
            storage = "run"
        config_files = getattr(root, "config_file", None) or []
        if isinstance(config_files, str):
            config_files = [config_files]
        repo = None
        if config_files:
            repo = repository_state(
                Path(config_files[0]).resolve().parent, self.config.tracked_ref
            )
        version = getattr(self.app_state, "terraform_version", None)
        if isinstance(version, tuple):
            version = ".".join(str(part) for part in version)
        return Provenance(
            labels=parse_labels(getattr(root, "run_label", None)),
            intent=getattr(tf, "intent", None),
            plan_storage=storage,
            repository=repo,
            worker_version=_worker_version(),
            terraform_version=str(version) if version else None,
            host=socket.gethostname(),
            user=_user(),
        )

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
        day = record.started_at.strftime("%Y/%m/%d")
        path = f"runs/{day}/{record.run_id}.json"
        try:
            self.backend.write_report(
                path, record.model_dump_json(by_alias=True), create_only=True
            )
            log.info(f"run record written: {path}")
        except ReportConflictError:
            log.warn(f"a run record for {record.run_id} already exists; not replaced")
        except BackendError as e:
            log.warn(f"unable to write the run record {path}: {e}")

    def _write_status(self, record: RunRecord, result: DefinitionResult) -> None:
        path = f"status/{result.definition}.json"
        for _ in range(self.config.status_retries):
            try:
                found = self.backend.read_report(path)
                current, etag = None, None
                if found is not None:
                    text, etag = found
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


def _worker_version() -> Optional[str]:
    try:
        return metadata.version("terraform-worker")
    except metadata.PackageNotFoundError:
        return None


def _user() -> Optional[str]:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return None
