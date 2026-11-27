"""
Read-only views over the projection.

Each function takes a session and a `Scope` and returns the API's response
models, which the JSON API serves as they are and the screens render.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from tfworker.backends.lock import LockInfo, WorkerLease
from tfworker.report.models import LOCAL_MODULE_TAG

from . import db

Health = Literal["failing", "drifted", "pending", "locked", "ok"]
# exceptions first, in this order
HEALTH_ORDER: Dict[str, int] = {
    "failing": 0,
    "drifted": 1,
    "pending": 2,
    "locked": 3,
    "ok": 4,
}


@dataclass
class Scope:
    """Narrows every view to some stores and deployments; empty means all."""

    stores: List[str] = field(default_factory=list)
    deployments: List[str] = field(default_factory=list)

    def where(self, model):
        clauses = []
        if self.stores:
            clauses.append(model.store.in_(self.stores))
        if self.deployments:
            clauses.append(model.deployment.in_(self.deployments))
        return clauses


class Event(BaseModel):
    run_id: str
    at: datetime
    status: Optional[str] = None
    add: int = 0
    change: int = 0
    destroy: int = 0
    reason: Optional[str] = None


class DefinitionHealth(BaseModel):
    store: str
    deployment: str
    definition: str
    health: Health
    last_plan: Optional[Event] = None
    last_apply: Optional[Event] = None
    last_failure: Optional[Event] = None
    pending_plan: Optional[Event] = None
    drift: Optional[Event] = None
    locked: bool = False


class Fleet(BaseModel):
    counts: Dict[str, int]
    exceptions: List[DefinitionHealth]


class DeploymentSummary(BaseModel):
    store: str
    # the prefix segment, which identifies the deployment
    deployment: str
    # the name its newest run record gives, when that differs
    record_name: Optional[str] = None
    prefix: str
    last_seen: datetime
    definitions: int
    counts: Dict[str, int]


class LockView(BaseModel):
    store: str
    deployment: str
    definition: str
    lock_id: str
    who: str
    operation: str
    created: datetime
    age_seconds: float
    state: Literal["live", "stale"]
    lease: Optional[dict] = None
    observed_at: datetime


class PlanView(BaseModel):
    store: str
    deployment: str
    definition: str
    plan_id: str
    run_id: str
    status: str
    storage: str
    destroy: bool
    created_at: datetime
    expires_at: Optional[datetime] = None
    add: int = 0
    change: int = 0
    destroy_count: int = 0


class RunView(BaseModel):
    store: str
    deployment: str
    record_id: str
    run_id: str
    status: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    actions: List[str]
    intent: Optional[str] = None
    definitions: List[str]


class ModuleVersion(BaseModel):
    store: str
    deployment: str
    source: str
    kind: str
    ref: Optional[str] = None
    sha: Optional[str] = None


class ModuleSkew(BaseModel):
    key: str
    skewed: bool
    versions: List[ModuleVersion]


class DefinitionDeployment(BaseModel):
    health: DefinitionHealth
    pending_plan: Optional[PlanView] = None
    previews: List[PlanView]
    lock: Optional[LockView] = None
    recent_runs: List[RunView]


class DefinitionView(BaseModel):
    definition: str
    deployments: List[DefinitionDeployment]
    modules: List[ModuleSkew]


class TimelineEntry(BaseModel):
    at: datetime
    store: str
    deployment: str
    kind: Literal["phase", "plan"]
    run_id: Optional[str] = None
    action: str
    status: str
    detail: Optional[str] = None


class WeekShare(BaseModel):
    week: str
    applies: int
    local: int
    share: float


def _event(data: Optional[dict]) -> Optional[Event]:
    if not data:
        return None
    changes = data.get("changes") or {}
    return Event(
        run_id=data["run_id"],
        at=data["at"],
        status=data.get("status"),
        add=changes.get("add", 0),
        change=changes.get("change", 0),
        destroy=changes.get("destroy", 0),
        reason=data.get("reason"),
    )


def _health(body: dict, locked: bool) -> Health:
    failure, applied = body.get("last_failure"), body.get("last_apply")
    if failure and (not applied or failure["at"] > applied["at"]):
        return "failing"
    if body.get("drift"):
        return "drifted"
    if body.get("pending_plan"):
        return "pending"
    if locked:
        return "locked"
    return "ok"


def _healths(session: Session, scope: Scope) -> List[DefinitionHealth]:
    locks = {
        (row.store, row.deployment, row.definition)
        for row in session.scalars(select(db.Lock).where(*scope.where(db.Lock)))
    }
    healths = {}
    for row in session.scalars(
        select(db.Definition).where(*scope.where(db.Definition))
    ):
        key = (row.store, row.deployment, row.name)
        healths[key] = DefinitionHealth(
            store=row.store,
            deployment=row.deployment,
            definition=row.name,
            health="locked" if key in locks else "ok",
            locked=key in locks,
        )
    for row in session.scalars(select(db.Status).where(*scope.where(db.Status))):
        key = (row.store, row.deployment, row.definition)
        body = row.body
        healths[key] = DefinitionHealth(
            store=row.store,
            deployment=row.deployment,
            definition=row.definition,
            health=_health(body, key in locks),
            last_plan=_event(body.get("last_plan")),
            last_apply=_event(body.get("last_apply")),
            last_failure=_event(body.get("last_failure")),
            pending_plan=_event(body.get("pending_plan")),
            drift=_event(body.get("drift")),
            locked=key in locks,
        )
    return sorted(
        healths.values(),
        key=lambda h: (HEALTH_ORDER[h.health], h.store, h.deployment, h.definition),
    )


def _count(healths: List[DefinitionHealth]) -> Dict[str, int]:
    counts = {health: 0 for health in HEALTH_ORDER}
    for item in healths:
        counts[item.health] += 1
    return counts


def fleet(session: Session, scope: Scope) -> Fleet:
    healths = _healths(session, scope)
    return Fleet(
        counts=_count(healths),
        exceptions=[h for h in healths if h.health != "ok"],
    )


def deployments(session: Session, scope: Scope) -> List[DeploymentSummary]:
    by_deployment: Dict[Tuple[str, str], List[DefinitionHealth]] = defaultdict(list)
    for item in _healths(session, scope):
        by_deployment[(item.store, item.deployment)].append(item)
    query = select(db.Deployment).order_by(db.Deployment.store, db.Deployment.name)
    if scope.stores:
        query = query.where(db.Deployment.store.in_(scope.stores))
    if scope.deployments:
        query = query.where(db.Deployment.name.in_(scope.deployments))
    result = []
    for row in session.scalars(query):
        items = by_deployment[(row.store, row.name)]
        result.append(
            DeploymentSummary(
                store=row.store,
                deployment=row.name,
                record_name=row.record_name,
                prefix=row.prefix,
                last_seen=row.last_seen,
                definitions=len(items),
                counts=_count(items),
            )
        )
    return result


def _lock_view(row: db.Lock, now: datetime, stale_after: float) -> LockView:
    lock = LockInfo(
        ID=row.lock_id, Operation=row.operation, Who=row.who, Created=row.created
    )
    state = "live"
    lease = WorkerLease.model_validate(row.lease) if row.lease else None
    if lease is not None and lease.holds(lock):
        if not lease.is_fresh(now):
            state = "stale"
    elif lock.age(now) > stale_after:
        state = "stale"
    return LockView(
        store=row.store,
        deployment=row.deployment,
        definition=row.definition,
        lock_id=row.lock_id,
        who=row.who,
        operation=row.operation,
        created=lock.created,
        age_seconds=round(max(lock.age(now), 0), 1),
        state=state,
        lease=row.lease,
        observed_at=row.observed_at,
    )


def locks(
    session: Session,
    scope: Scope,
    now: datetime,
    stale_after: float,
    state: Optional[str] = None,
) -> List[LockView]:
    query = select(db.Lock).where(*scope.where(db.Lock)).order_by(db.Lock.created)
    views = [_lock_view(row, now, stale_after) for row in session.scalars(query)]
    return [view for view in views if not state or view.state == state]


def _plan_view(row: db.Plan) -> PlanView:
    body = row.body
    changes = body.get("changes") or {}
    return PlanView(
        store=row.store,
        deployment=row.deployment,
        definition=row.definition,
        plan_id=row.plan_id,
        run_id=body["run_id"],
        status=row.status,
        storage=body.get("storage", "backend"),
        destroy=body.get("destroy", False),
        created_at=row.created_at,
        expires_at=body.get("expires_at"),
        add=changes.get("add", 0),
        change=changes.get("change", 0),
        destroy_count=changes.get("destroy", 0),
    )


def plans(
    session: Session,
    scope: Scope,
    status: Optional[str] = None,
    definition: Optional[str] = None,
    limit: int = 200,
) -> List[PlanView]:
    query = select(db.Plan).where(*scope.where(db.Plan))
    if status:
        query = query.where(db.Plan.status == status)
    if definition:
        query = query.where(db.Plan.definition == definition)
    query = query.order_by(db.Plan.created_at.desc()).limit(limit)
    return [_plan_view(row) for row in session.scalars(query)]


def _run_view(row: db.Run) -> RunView:
    body = row.body
    return RunView(
        store=row.store,
        deployment=row.deployment,
        record_id=row.record_id,
        run_id=row.run_id,
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
        actions=body.get("actions", []),
        intent=(body.get("provenance") or {}).get("intent"),
        definitions=[d["definition"] for d in body.get("definitions", [])],
    )


def runs(
    session: Session,
    scope: Scope,
    status: Optional[str] = None,
    definition: Optional[str] = None,
    limit: int = 100,
) -> List[RunView]:
    query = select(db.Run).where(*scope.where(db.Run))
    if status:
        query = query.where(db.Run.status == status)
    query = query.order_by(db.Run.started_at.desc())
    result = []
    for row in session.scalars(query):
        view = _run_view(row)
        if definition and definition not in view.definitions:
            continue
        result.append(view)
        if len(result) >= limit:
            break
    return result


def run(session: Session, store: str, record_id: str) -> Optional[dict]:
    row = session.get(db.Run, (store, record_id))
    return row.body if row else None


def definition(
    session: Session,
    scope: Scope,
    name: str,
    now: datetime,
    stale_after: float,
    recent: int = 5,
) -> Optional[DefinitionView]:
    healths = [h for h in _healths(session, scope) if h.definition == name]
    if not healths:
        return None
    plan_rows = [
        _plan_view(row)
        for row in session.scalars(
            select(db.Plan)
            .where(db.Plan.definition == name, *scope.where(db.Plan))
            .order_by(db.Plan.created_at.desc())
        )
    ]
    lock_rows = {
        (row.store, row.deployment): _lock_view(row, now, stale_after)
        for row in session.scalars(
            select(db.Lock).where(db.Lock.definition == name, *scope.where(db.Lock))
        )
    }
    deployments_ = []
    modules: Dict[str, List[ModuleVersion]] = defaultdict(list)
    for health in sorted(healths, key=lambda h: (h.store, h.deployment)):
        here = [
            p
            for p in plan_rows
            if (p.store, p.deployment) == (health.store, health.deployment)
        ]
        recent_runs = runs(
            session,
            Scope([health.store], [health.deployment]),
            definition=name,
            limit=recent,
        )
        deployments_.append(
            DefinitionDeployment(
                health=health,
                pending_plan=next(
                    (
                        p
                        for p in here
                        if p.status == "pending" and p.storage != "preview"
                    ),
                    None,
                ),
                previews=[p for p in here if p.storage == "preview"][:recent],
                lock=lock_rows.get((health.store, health.deployment)),
                recent_runs=recent_runs,
            )
        )
        for source in _module_sources(session, health.store, health.deployment, name):
            modules[source["key"]].append(
                ModuleVersion(
                    store=health.store,
                    deployment=health.deployment,
                    source=source["source"],
                    kind=source["kind"],
                    ref=source.get("ref"),
                    sha=source.get("sha"),
                )
            )
    skews = [
        ModuleSkew(
            key=key,
            skewed=len({(v.source, v.ref, v.sha) for v in versions}) > 1,
            versions=versions,
        )
        for key, versions in sorted(modules.items())
    ]
    return DefinitionView(definition=name, deployments=deployments_, modules=skews)


def _module_sources(session, store, deployment, name) -> List[dict]:
    """The module sources from the latest run that recorded this definition."""
    query = (
        select(db.Run)
        .where(db.Run.store == store, db.Run.deployment == deployment)
        .order_by(db.Run.started_at.desc())
    )
    for row in session.scalars(query):
        for result in row.body.get("definitions", []):
            if result["definition"] == name and result.get("module_sources"):
                return result["module_sources"]
    return []


def timeline(
    session: Session, scope: Scope, name: str, limit: int = 200
) -> List[TimelineEntry]:
    entries: List[TimelineEntry] = []
    for row in session.scalars(select(db.Run).where(*scope.where(db.Run))):
        for result in row.body.get("definitions", []):
            if result["definition"] != name:
                continue
            for action, phase in result.get("phases", {}).items():
                at = phase.get("finished_at") or phase.get("started_at")
                if not at:
                    continue
                entries.append(
                    TimelineEntry(
                        at=at,
                        store=row.store,
                        deployment=row.deployment,
                        kind="phase",
                        run_id=row.run_id,
                        action=action,
                        status=phase["status"],
                        detail=phase.get("reason"),
                    )
                )
    for row in session.scalars(
        select(db.Plan).where(db.Plan.definition == name, *scope.where(db.Plan))
    ):
        for change in row.body.get("history", []):
            entries.append(
                TimelineEntry(
                    at=change["at"],
                    store=row.store,
                    deployment=row.deployment,
                    kind="plan",
                    run_id=change.get("run_id"),
                    action=f"plan {row.plan_id}",
                    status=change["status"],
                    detail=change.get("reason"),
                )
            )
    entries.sort(key=lambda e: e.at, reverse=True)
    return entries[:limit]


def _week(at: datetime) -> str:
    year, week, _ = at.isocalendar()
    return f"{year}-W{week:02d}"


def local_module_applies(
    session: Session, scope: Scope, now: datetime, weeks: int = 12
) -> List[WeekShare]:
    """Per ISO week, how many definition applies used a module from a local path."""
    since = now - timedelta(weeks=weeks)
    totals: Dict[str, List[int]] = {}
    cursor = since
    while cursor <= now:
        totals.setdefault(_week(cursor), [0, 0])
        cursor += timedelta(days=1)
    query = select(db.Run).where(db.Run.started_at >= since, *scope.where(db.Run))
    for row in session.scalars(query):
        for result in row.body.get("definitions", []):
            phase = result.get("phases", {}).get("apply")
            if not phase or phase.get("status") == "skipped":
                continue
            started = row.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            week = totals.setdefault(_week(started), [0, 0])
            week[0] += 1
            if LOCAL_MODULE_TAG in result.get("tags", []):
                week[1] += 1
    return [
        WeekShare(
            week=week,
            applies=applies,
            local=local,
            share=round(local / applies, 4) if applies else 0.0,
        )
        for week, (applies, local) in sorted(totals.items())
    ]
