"""
Versioned schema for run records (`tfworker.run/v1`) and per-definition
status (`tfworker.status/v1`).

Records hold addresses, actions, counts and timings only. They never carry
attribute values from plans or state, which can be sensitive.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

RUN_SCHEMA = "tfworker.run/v1"
STATUS_SCHEMA = "tfworker.status/v1"

PhaseName = Literal["init", "plan", "apply", "destroy"]
PhaseStatus = Literal["succeeded", "failed", "skipped"]
Intent = Literal["deploy", "preview", "drift"]
PlanStorage = Literal["backend", "local", "run"]

# set on a result whose definition was applied while a module came from a
# local path rather than a reviewed source; a trend to track, not an alert
LOCAL_MODULE_TAG = "local-module-paths"


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class BackendLocation(_Model):
    """Where a definition's state lives. A state is identified by this, not by name."""

    backend: str
    account_id: Optional[str] = None
    region: Optional[str] = None
    bucket: Optional[str] = None
    state_key: str

    @property
    def identity(self) -> str:
        parts = [
            self.backend,
            self.account_id or "",
            self.region or "",
            self.bucket or "",
        ]
        return "/".join(parts) + f"/{self.state_key}"


class ResourceChange(_Model):
    address: str
    actions: List[str]
    importing: bool = False


class ChangeSummary(_Model):
    """Counts as terraform prints them, plus the address and actions of each change."""

    add: int = 0
    change: int = 0
    destroy: int = 0
    import_: int = Field(0, alias="import")
    resources: List[ResourceChange] = Field(default_factory=list)

    @property
    def total(self) -> int:
        return self.add + self.change + self.destroy + self.import_

    @classmethod
    def from_plan_json(cls, plan: Dict[str, Any]) -> "ChangeSummary":
        """Summarize `terraform show -json` output, keeping no attribute values."""
        summary = cls()
        for rc in plan.get("resource_changes") or []:
            change = rc.get("change") or {}
            actions = list(change.get("actions") or [])
            importing = change.get("importing") is not None
            if actions in (["no-op"], ["read"]) and not importing:
                continue
            if "create" in actions:
                summary.add += 1
            if "delete" in actions:
                summary.destroy += 1
            if "update" in actions:
                summary.change += 1
            if importing:
                summary.import_ += 1
            summary.resources.append(
                ResourceChange(
                    address=rc.get("address", ""), actions=actions, importing=importing
                )
            )
        return summary

    def counts_only(self) -> "ChangeSummary":
        return self.model_copy(update={"resources": []})


class PhaseResult(_Model):
    action: PhaseName
    status: PhaseStatus
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    exit_code: Optional[int] = None
    reason: Optional[str] = None
    lock_wait_seconds: Optional[float] = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def _aware(cls, value: Optional[datetime]) -> Optional[datetime]:
        return _utc(value)


class ModuleSource(_Model):
    """
    A module terraform installed for a definition, as recorded in modules.json.

    kind "local" is a path outside the definition, such as a mounted module
    checkout; "bundled" is a path inside the definition or inside a module
    fetched from git or a registry.
    """

    key: str
    source: str
    kind: Literal["git", "local", "bundled", "registry", "other"]
    ref: Optional[str] = None
    sha: Optional[str] = None


class RepositoryState(_Model):
    """The git checkout the worker ran from."""

    sha: Optional[str] = None
    branch: Optional[str] = None
    dirty: Optional[bool] = None
    tracked_ref: Optional[str] = None
    on_tracked_ref: Optional[bool] = None


class Provenance(_Model):
    """Observable facts about where a run came from, recorded in place of intent."""

    labels: Dict[str, str] = Field(default_factory=dict)
    intent: Optional[Intent] = None
    plan_storage: PlanStorage = "run"
    repository: Optional[RepositoryState] = None
    worker_version: Optional[str] = None
    terraform_version: Optional[str] = None
    host: Optional[str] = None
    user: Optional[str] = None


class DefinitionResult(_Model):
    definition: str
    state: BackendLocation
    phases: Dict[PhaseName, PhaseResult] = Field(default_factory=dict)
    changes: Optional[ChangeSummary] = None
    base_lineage: Optional[str] = None
    base_serial: Optional[int] = None
    applied_serial: Optional[int] = None
    module_sources: List[ModuleSource] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(p.status == "failed" for p in self.phases.values())


class RunRecord(_Model):
    """One immutable record per run, written when the run ends."""

    schema_: Literal["tfworker.run/v1"] = Field(RUN_SCHEMA, alias="schema")
    run_id: str
    run_id_generated: bool = False
    deployment: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    status: Literal["succeeded", "failed"]
    actions: List[Literal["plan", "apply", "destroy"]] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)
    definitions: List[DefinitionResult] = Field(default_factory=list)

    @field_validator("started_at", "finished_at")
    @classmethod
    def _aware(cls, value: Optional[datetime]) -> Optional[datetime]:
        return _utc(value)


class RunEvent(_Model):
    """One phase of one run, as it appears in a status snapshot."""

    run_id: str
    action: PhaseName
    status: PhaseStatus
    at: datetime
    duration_seconds: Optional[float] = None
    changes: Optional[ChangeSummary] = None
    base_lineage: Optional[str] = None
    base_serial: Optional[int] = None
    applied_serial: Optional[int] = None
    plan_storage: Optional[PlanStorage] = None
    reason: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    labels: Dict[str, str] = Field(default_factory=dict)

    @field_validator("at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)


class DefinitionStatus(_Model):
    """The latest known state of one definition, rewritten by each run."""

    schema_: Literal["tfworker.status/v1"] = Field(STATUS_SCHEMA, alias="schema")
    deployment: str
    definition: str
    state: BackendLocation
    updated_at: datetime
    updated_by_run: str
    last_plan: Optional[RunEvent] = None
    last_apply: Optional[RunEvent] = None
    last_failure: Optional[RunEvent] = None
    pending_plan: Optional[RunEvent] = None
    drift: Optional[RunEvent] = None

    @field_validator("updated_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)
