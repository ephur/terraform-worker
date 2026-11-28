"""
Read-only views over the projection.

Each function takes a session and a `Scope` and returns the API's response
models, which the JSON API serves as they are and the screens render.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Literal, Optional, Set, Tuple

from pydantic import BaseModel
from sqlalchemy import and_, false, select, tuple_
from sqlalchemy.orm import Session
from tfworker.backends.lock import LockInfo, WorkerLease

from . import db
from .groups import Groups

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
    """
    Narrows every view to some stores, deployments and groups; empty means all.

    Groups filter in SQL on each deployment's stored group, so a scope works
    the same however many deployments a group holds. ``resolver`` places
    deployments in groups for display and policy.
    """

    stores: List[str] = field(default_factory=list)
    deployments: List[str] = field(default_factory=list)
    groups: List[str] = field(default_factory=list)
    resolver: Groups = field(default_factory=Groups)
    # the groups the caller may see; None means every group
    allowed: Optional[Set[str]] = None

    @property
    def wanted(self) -> Optional[List[str]]:
        """The groups to filter to, None for no group filter."""
        if self.allowed is None:
            return self.groups or None
        return [
            name
            for name in (self.groups or sorted(self.allowed))
            if name in self.allowed
        ]

    def where(self, model, deployment=None):
        deployment = deployment if deployment is not None else model.deployment
        clauses = []
        if self.stores:
            clauses.append(model.store.in_(self.stores))
        if self.deployments:
            clauses.append(deployment.in_(self.deployments))
        wanted = self.wanted
        if wanted is not None:
            if not wanted:
                clauses.append(false())
            elif model is db.Deployment:
                clauses.append(db.Deployment.group_name.in_(wanted))
            else:
                members = select(db.Deployment.store, db.Deployment.name).where(
                    db.Deployment.group_name.in_(wanted)
                )
                clauses.append(tuple_(model.store, deployment).in_(members))
        return clauses

    def group(self, store: str, deployment: str) -> str:
        return self.resolver.of(store, deployment)


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
    group: str
    definition: str
    health: Health
    last_plan: Optional[Event] = None
    last_apply: Optional[Event] = None
    last_failure: Optional[Event] = None
    pending_plan: Optional[Event] = None
    drift: Optional[Event] = None
    locked: bool = False


class GroupView(BaseModel):
    name: str
    plan_ttl: Optional[float] = None
    force_unlock: Literal["operators", "admins"]
    deployments: List[str]
    counts: Dict[str, int]


class Fleet(BaseModel):
    counts: Dict[str, int]
    exceptions: List[DefinitionHealth]


class DeploymentSummary(BaseModel):
    store: str
    group: str
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
    group: str
    definition: str
    lock_id: str
    who: str
    operation: str
    created: datetime
    age_seconds: float
    state: Literal["live", "stale"]
    lease: Optional[dict] = None
    # the state is judged as of the last reconcile that saw the lock
    observed_at: datetime
    observed_seconds_ago: float
    # who the group's policy lets force-unlock it
    force_unlock: Literal["operators", "admins"]


class PlanView(BaseModel):
    store: str
    deployment: str
    group: str
    definition: str
    plan_id: str
    run_id: str
    status: str
    storage: str
    destroy: bool
    created_at: datetime
    expires_at: Optional[datetime] = None
    # pending past the manifest's expiry; the worker will refuse to apply it
    expired: bool = False
    # when the plan passes its group's plan TTL, and whether it has; the
    # worker still applies it, so it stays pending and is flagged for cleanup
    ttl_expires_at: Optional[datetime] = None
    over_ttl: bool = False
    add: int = 0
    change: int = 0
    destroy_count: int = 0


class RunView(BaseModel):
    store: str
    deployment: str
    group: str
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


def _failing(body: dict) -> bool:
    """
    Whether the newest failure is newer than every success since.

    The worker records every apply in last_apply whatever its outcome, so a
    failed apply is both the last apply and the last failure; only a newer
    succeeded plan or apply clears the failure.
    """
    failure = _event(body.get("last_failure"))
    if failure is None:
        return False
    successes = [
        event.at
        for event in (_event(body.get("last_apply")), _event(body.get("last_plan")))
        if event is not None and event.status == "succeeded"
    ]
    return not successes or failure.at >= max(successes)


def _health(body: dict, locked: bool, pending: bool) -> Health:
    if _failing(body):
        return "failing"
    if body.get("drift"):
        return "drifted"
    if pending:
        return "pending"
    if locked:
        return "locked"
    return "ok"


def _pending_keys(session: Session, scope: Scope, now: datetime) -> set:
    """Definitions with a saved plan that is still pending and unexpired."""
    query = select(db.Plan).where(db.Plan.status == "pending", *scope.where(db.Plan))
    return {
        (row.store, row.deployment, row.definition)
        for row in session.scalars(query)
        if row.body.get("storage") != "preview" and not _plan_expired(row, now)
    }


def _healths(session: Session, scope: Scope, now: datetime) -> List[DefinitionHealth]:
    locks = {
        (row.store, row.deployment, row.definition)
        for row in session.scalars(select(db.Lock).where(*scope.where(db.Lock)))
    }
    pending = _pending_keys(session, scope, now)
    statuses = {
        (row.store, row.deployment, row.definition): row.body
        for row in session.scalars(select(db.Status).where(*scope.where(db.Status)))
    }
    keys = {
        (row.store, row.deployment, row.name)
        for row in session.scalars(
            select(db.Definition).where(*scope.where(db.Definition))
        )
    } | set(statuses)
    healths = []
    for key in keys:
        store, deployment, name = key
        body = statuses.get(key, {})
        healths.append(
            DefinitionHealth(
                store=store,
                deployment=deployment,
                group=scope.group(store, deployment),
                definition=name,
                health=_health(body, key in locks, key in pending),
                last_plan=_event(body.get("last_plan")),
                last_apply=_event(body.get("last_apply")),
                last_failure=_event(body.get("last_failure")),
                pending_plan=(
                    _event(body.get("pending_plan")) if key in pending else None
                ),
                drift=_event(body.get("drift")),
                locked=key in locks,
            )
        )
    return sorted(
        healths,
        key=lambda h: (
            HEALTH_ORDER[h.health],
            scope.resolver.rank(h.group),
            h.store,
            h.deployment,
            h.definition,
        ),
    )


def _count(healths: List[DefinitionHealth]) -> Dict[str, int]:
    counts = {health: 0 for health in HEALTH_ORDER}
    for item in healths:
        counts[item.health] += 1
    return counts


def fleet(session: Session, scope: Scope, now: datetime) -> Fleet:
    healths = _healths(session, scope, now)
    return Fleet(
        counts=_count(healths),
        exceptions=[h for h in healths if h.health != "ok"],
    )


def deployments(
    session: Session, scope: Scope, now: datetime
) -> List[DeploymentSummary]:
    by_deployment: Dict[Tuple[str, str], List[DefinitionHealth]] = defaultdict(list)
    for item in _healths(session, scope, now):
        by_deployment[(item.store, item.deployment)].append(item)
    query = select(db.Deployment).where(*scope.where(db.Deployment, db.Deployment.name))
    result = []
    for row in session.scalars(query):
        items = by_deployment[(row.store, row.name)]
        result.append(
            DeploymentSummary(
                store=row.store,
                group=scope.group(row.store, row.name),
                deployment=row.name,
                record_name=row.record_name,
                prefix=row.prefix,
                last_seen=row.last_seen,
                definitions=len(items),
                counts=_count(items),
            )
        )
    result.sort(key=lambda d: (scope.resolver.rank(d.group), d.store, d.deployment))
    return result


def groups(session: Session, scope: Scope, now: datetime) -> List[GroupView]:
    """Every group in sort order with its policy, members and health counts."""
    # a group scope picks which groups are listed; each lists all its members
    unscoped = Scope(
        scope.stores,
        scope.deployments,
        resolver=scope.resolver,
        allowed=scope.allowed,
    )
    members: Dict[str, List[DeploymentSummary]] = defaultdict(list)
    for summary in deployments(session, unscoped, now):
        members[summary.group].append(summary)
    result = []
    for name in scope.resolver.names:
        if scope.groups and name not in scope.groups:
            continue
        if scope.allowed is not None and name not in scope.allowed:
            continue
        group = scope.resolver.group(name)
        counts = {health: 0 for health in HEALTH_ORDER}
        for summary in members[name]:
            for health, n in summary.counts.items():
                counts[health] += n
        result.append(
            GroupView(
                name=name,
                plan_ttl=group.plan_ttl,
                force_unlock=group.force_unlock,
                deployments=[f"{d.store}/{d.deployment}" for d in members[name]],
                counts=counts,
            )
        )
    return result


def _lock_view(
    row: db.Lock, now: datetime, stale_after: float, scope: Scope
) -> LockView:
    """
    Judge the lock as it was observed: the lease is a snapshot from the last
    reconcile, so comparing it with the time of the request would call every
    healthy lock stale once the snapshot ages past the freshness window.
    """
    lock = LockInfo(
        ID=row.lock_id, Operation=row.operation, Who=row.who, Created=row.created
    )
    seen = row.observed_at
    state = "live"
    lease = WorkerLease.model_validate(row.lease) if row.lease else None
    if lease is not None and lease.holds(lock):
        if not lease.is_fresh(seen):
            state = "stale"
    elif lock.age(seen) > stale_after:
        state = "stale"
    policy = scope.resolver.policy(row.store, row.deployment)
    return LockView(
        store=row.store,
        deployment=row.deployment,
        group=policy.name,
        force_unlock=policy.force_unlock,
        definition=row.definition,
        lock_id=row.lock_id,
        who=row.who,
        operation=row.operation,
        created=lock.created,
        age_seconds=round(max(lock.age(now), 0), 1),
        state=state,
        lease=row.lease,
        observed_at=seen,
        observed_seconds_ago=round(max((now - seen).total_seconds(), 0), 1),
    )


def locks(
    session: Session,
    scope: Scope,
    now: datetime,
    stale_after: float,
    state: Optional[str] = None,
) -> List[LockView]:
    query = select(db.Lock).where(*scope.where(db.Lock)).order_by(db.Lock.created)
    views = [_lock_view(row, now, stale_after, scope) for row in session.scalars(query)]
    return [view for view in views if not state or view.state == state]


def _plan_expired(row: db.Plan, now: datetime) -> bool:
    """Pending past the manifest's own expiry, so the worker will refuse it."""
    expires_at = row.body.get("expires_at")
    return (
        row.status == "pending"
        and expires_at is not None
        and now >= datetime.fromisoformat(expires_at)
    )


def _ttl_expires(row: db.Plan, scope: Scope) -> Optional[datetime]:
    """When a saved plan passes its group's plan TTL; previews have none."""
    policy = scope.resolver.policy(row.store, row.deployment)
    if policy.plan_ttl is None or row.body.get("storage") == "preview":
        return None
    return row.created_at + timedelta(seconds=policy.plan_ttl)


def _plan_view(row: db.Plan, now: datetime, scope: Scope) -> PlanView:
    body = row.body
    changes = body.get("changes") or {}
    ttl_expires_at = _ttl_expires(row, scope)
    return PlanView(
        store=row.store,
        deployment=row.deployment,
        group=scope.group(row.store, row.deployment),
        definition=row.definition,
        plan_id=row.plan_id,
        run_id=body["run_id"],
        status=row.status,
        storage=body.get("storage", "backend"),
        destroy=body.get("destroy", False),
        created_at=row.created_at,
        expires_at=body.get("expires_at"),
        expired=_plan_expired(row, now),
        ttl_expires_at=ttl_expires_at,
        over_ttl=row.status == "pending"
        and ttl_expires_at is not None
        and now >= ttl_expires_at,
        add=changes.get("add", 0),
        change=changes.get("change", 0),
        destroy_count=changes.get("destroy", 0),
    )


def plans(
    session: Session,
    scope: Scope,
    now: datetime,
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
    return [_plan_view(row, now, scope) for row in session.scalars(query)]


def _run_view(row: db.Run, scope: Scope) -> RunView:
    body = row.body
    return RunView(
        store=row.store,
        deployment=row.deployment,
        group=scope.group(row.store, row.deployment),
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
    if definition:
        query = query.join(
            db.RunDefinition,
            and_(
                db.RunDefinition.store == db.Run.store,
                db.RunDefinition.record_id == db.Run.record_id,
            ),
        ).where(db.RunDefinition.definition == definition)
    query = query.order_by(db.Run.started_at.desc(), db.Run.record_id.desc())
    return [_run_view(row, scope) for row in session.scalars(query.limit(limit))]


def definition(
    session: Session,
    scope: Scope,
    name: str,
    now: datetime,
    stale_after: float,
    recent: int = 5,
    history: int = 500,
) -> Optional[DefinitionView]:
    healths = [h for h in _healths(session, scope, now) if h.definition == name]
    if not healths:
        return None
    plan_rows = [
        _plan_view(row, now, scope)
        for row in session.scalars(
            select(db.Plan)
            .where(db.Plan.definition == name, *scope.where(db.Plan))
            .order_by(db.Plan.created_at.desc())
            .limit(history)
        )
    ]
    lock_rows = {
        (row.store, row.deployment): _lock_view(row, now, stale_after, scope)
        for row in session.scalars(
            select(db.Lock).where(db.Lock.definition == name, *scope.where(db.Lock))
        )
    }
    # the recent runs and module sources of every deployment, in one query
    recent_runs: Dict[Tuple[str, str], List[RunView]] = defaultdict(list)
    sources: Dict[Tuple[str, str], List[dict]] = {}
    for part, row in session.execute(
        select(db.RunDefinition, db.Run)
        .join(
            db.Run,
            and_(
                db.Run.store == db.RunDefinition.store,
                db.Run.record_id == db.RunDefinition.record_id,
            ),
        )
        .where(db.RunDefinition.definition == name, *scope.where(db.RunDefinition))
        .order_by(db.RunDefinition.started_at.desc(), db.RunDefinition.record_id.desc())
        .limit(history)
    ):
        key = (part.store, part.deployment)
        if len(recent_runs[key]) < recent:
            recent_runs[key].append(_run_view(row, scope))
        if key not in sources and part.module_sources:
            sources[key] = part.module_sources
    deployments_ = []
    modules: Dict[str, List[ModuleVersion]] = defaultdict(list)
    for health in sorted(healths, key=lambda h: (h.store, h.deployment)):
        key = (health.store, health.deployment)
        here = [p for p in plan_rows if (p.store, p.deployment) == key]
        deployments_.append(
            DefinitionDeployment(
                health=health,
                pending_plan=next(
                    (
                        p
                        for p in here
                        if p.status == "pending"
                        and p.storage != "preview"
                        and not p.expired
                    ),
                    None,
                ),
                previews=[p for p in here if p.storage == "preview"][:recent],
                lock=lock_rows.get(key),
                recent_runs=recent_runs[key],
            )
        )
        for source in sources.get(key, []):
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


def timeline(
    session: Session, scope: Scope, name: str, limit: int = 200
) -> List[TimelineEntry]:
    """The newest phases and plan status changes; each source is read up to `limit`."""
    entries: List[TimelineEntry] = []
    for part, run_id in session.execute(
        select(db.RunDefinition, db.Run.run_id)
        .join(
            db.Run,
            and_(
                db.Run.store == db.RunDefinition.store,
                db.Run.record_id == db.RunDefinition.record_id,
            ),
        )
        .where(db.RunDefinition.definition == name, *scope.where(db.RunDefinition))
        .order_by(db.RunDefinition.started_at.desc())
        .limit(limit)
    ):
        for action, phase in part.phases.items():
            at = phase.get("finished_at") or phase.get("started_at")
            if not at:
                continue
            entries.append(
                TimelineEntry(
                    at=at,
                    store=part.store,
                    deployment=part.deployment,
                    kind="phase",
                    run_id=run_id,
                    action=action,
                    status=phase["status"],
                    detail=phase.get("reason"),
                )
            )
    for row in session.scalars(
        select(db.Plan)
        .where(db.Plan.definition == name, *scope.where(db.Plan))
        .order_by(db.Plan.created_at.desc())
        .limit(limit)
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
    year, week, _ = at.astimezone(timezone.utc).isocalendar()
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
    query = select(
        db.RunDefinition.started_at,
        db.RunDefinition.phases,
        db.RunDefinition.local_modules,
    ).where(db.RunDefinition.started_at >= since, *scope.where(db.RunDefinition))
    for started_at, phases, local in session.execute(query):
        phase = phases.get("apply")
        if not phase or phase.get("status") == "skipped":
            continue
        week = totals.setdefault(_week(started_at), [0, 0])
        week[0] += 1
        if local:
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


class AuditView(BaseModel):
    at: datetime
    actor: str
    actor_kind: str
    action: str
    store: str
    deployment: str
    definition: str
    group: str
    target: str
    outcome: str
    reason: Optional[str] = None
    detail: Optional[str] = None


def audit(session: Session, scope: Scope, limit: int = 200) -> List[AuditView]:
    """
    Admin actions, newest first. Each entry keeps the group its deployment
    was in at the time, and is shown to whoever may see that group.
    """
    query = select(db.AuditEntry)
    if scope.stores:
        query = query.where(db.AuditEntry.store.in_(scope.stores))
    if scope.deployments:
        query = query.where(db.AuditEntry.deployment.in_(scope.deployments))
    wanted = scope.wanted
    if wanted is not None:
        query = query.where(db.AuditEntry.group_name.in_(wanted))
    query = query.order_by(db.AuditEntry.at.desc(), db.AuditEntry.id.desc())
    return [
        AuditView(
            at=row.at,
            actor=row.actor,
            actor_kind=row.actor_kind,
            action=row.action,
            store=row.store,
            deployment=row.deployment,
            definition=row.definition,
            group=row.group_name,
            target=row.target,
            outcome=row.outcome,
            reason=row.reason,
            detail=row.detail,
        )
        for row in session.scalars(query.limit(limit))
    ]
