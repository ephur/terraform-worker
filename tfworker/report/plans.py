"""
The manifest written beside every saved plan (`tfworker.plan/v1`).

A manifest records who made a plan, from what, against which state, and
whether anyone may still apply it. Its status moves through a small state
machine; the move from pending to applying is a conditional write, so only
one run can take a plan. A plan taken by a run that then stopped before
terraform started is released back to pending.
"""

from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import Field, field_validator

from .models import BackendLocation, ChangeSummary, Provenance, _Model, _utc

PLAN_SCHEMA = "tfworker.plan/v1"

PlanStatus = Literal[
    "pending",
    "applying",
    "applied",
    "failed",
    "stale",
    "superseded",
    "expired",
    "discarded",
]

TRANSITIONS = {
    "pending": {"applying", "stale", "superseded", "expired", "discarded"},
    "applying": {"applied", "failed", "pending"},
}


class InvalidTransition(ValueError):
    """A manifest cannot move from its status to the one asked for."""


class StatusChange(_Model):
    status: PlanStatus
    at: datetime
    run_id: Optional[str] = None
    reason: Optional[str] = None

    @field_validator("at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)


class PlanManifest(_Model):
    schema_: Literal["tfworker.plan/v1"] = Field(PLAN_SCHEMA, alias="schema")
    plan_id: str
    run_id: str
    deployment: str
    definition: str
    destroy: bool = False
    storage: Literal["backend", "local", "preview"] = "backend"
    plan_key: str
    state: Optional[BackendLocation] = None
    provenance: Provenance = Field(default_factory=Provenance)
    base_lineage: Optional[str] = None
    base_serial: Optional[int] = None
    base_state_version_id: Optional[str] = None
    sha256: str
    size: int
    changes: Optional[ChangeSummary] = None
    created_at: datetime
    expires_at: Optional[datetime] = None
    status: PlanStatus = "pending"
    history: List[StatusChange] = Field(default_factory=list)

    @field_validator("created_at", "expires_at")
    @classmethod
    def _aware(cls, value: Optional[datetime]) -> Optional[datetime]:
        return _utc(value)

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def can_move(self, status: str) -> bool:
        return status in TRANSITIONS.get(self.status, set())

    def moved(
        self,
        status: str,
        at: Optional[datetime] = None,
        run_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> "PlanManifest":
        """Return a copy in the new status, with the change recorded in history."""
        if not self.can_move(status):
            raise InvalidTransition(f"a {self.status} plan cannot become {status}")
        at = _utc(at) or datetime.now(timezone.utc)
        change = StatusChange(status=status, at=at, run_id=run_id, reason=reason)
        return self.model_copy(
            update={"status": status, "history": [*self.history, change]}, deep=True
        )
