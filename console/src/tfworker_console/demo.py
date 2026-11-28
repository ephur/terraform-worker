"""
Sample data for trying the console without AWS: two stores, a handful of
deployments, and runs, plans and locks that put every screen and every health
state to use. Records are projected the same way the reconciler projects a
store, so what you see is what real data would look like.
"""

import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from tfworker.report import (
    BackendLocation,
    ChangeSummary,
    DefinitionResult,
    ModuleSource,
    PhaseResult,
    PlanManifest,
    Provenance,
    RunRecord,
)
from tfworker.report.models import LOCAL_MODULE_TAG
from tfworker.report.status import merge_status

from . import db
from .groups import Group, Groups, GroupsFile, Rule
from .reconcile import PROJECTED, Reconciler

# demo stores carry this in place of a credentials kind, which no real store
# can have, so a seeded database can be told apart from a real one
MARKER = "demo"
STORES = {"demo-prod": "us-east-1", "demo-dev": "us-west-2"}
MODULES = "git::https://example.com/terraform-modules.git"

GROUPS = Groups(
    GroupsFile(
        groups=[
            Group(name="Production", plan_ttl="1d", force_unlock="admins"),
            Group(name="Sandbox", force_unlock="operators"),
        ],
        rules=[
            Rule(store="demo-prod", match="*", group="Production"),
            Rule(store="demo-dev", match="sandbox-*", group="Sandbox"),
        ],
    )
)


class DemoRefused(Exception):
    """The database already holds data that is not the demo's."""


def _stores_in(session: Session) -> set:
    """Every store any projected row belongs to."""
    stores = set(session.scalars(select(db.Store.name)))
    for model in PROJECTED + (db.Deployment,):
        stores |= set(session.scalars(select(model.store).distinct()))
    return stores


def _demo_stores(session: Session) -> set:
    return set(
        session.scalars(select(db.Store.name).where(db.Store.credentials == MARKER))
    )


def is_seeded(session: Session) -> bool:
    """Whether the database holds the demo, and nothing else."""
    stores = _stores_in(session)
    if not stores:
        return False
    if stores <= _demo_stores(session):
        return True
    raise DemoRefused(
        "the database already holds projected data; the demo only seeds an "
        "empty database"
    )


def forget(session: Session) -> List[str]:
    """Remove a demo left in the database, returning the stores removed."""
    gone = sorted(_demo_stores(session))
    for name in gone:
        for model in PROJECTED + (db.Deployment,):
            session.execute(delete(model).where(model.store == name))
        session.execute(delete(db.Store).where(db.Store.name == name))
    session.commit()
    return gone


def _phase(action: str, status: str, at: datetime, reason=None) -> PhaseResult:
    return PhaseResult(
        action=action,
        status=status,
        started_at=at,
        finished_at=at + timedelta(minutes=2),
        reason=reason,
    )


def _result(
    deployment: str,
    definition: str,
    phases: List[PhaseResult],
    ref: str = "v1.4.0",
    changes: Optional[ChangeSummary] = None,
    local: bool = False,
) -> DefinitionResult:
    return DefinitionResult(
        definition=definition,
        state=BackendLocation(
            backend="s3",
            state_key=f"terraform/state/{deployment}/{definition}/terraform.tfstate",
        ),
        phases={phase.action: phase for phase in phases},
        changes=changes,
        module_sources=[
            ModuleSource(
                key=definition,
                source="../modules/" + definition if local else MODULES,
                kind="local" if local else "git",
                ref=None if local else ref,
                sha=None if local else hashlib.sha1(ref.encode()).hexdigest(),
            )
        ],
        tags=[LOCAL_MODULE_TAG] if local else [],
    )


def _record(
    deployment: str,
    number: int,
    started: datetime,
    results: List[DefinitionResult],
    intent: str = "deploy",
) -> RunRecord:
    failed = any(p.status == "failed" for r in results for p in r.phases.values())
    actions = sorted({action for r in results for action in r.phases})
    return RunRecord(
        record_id=f"{deployment}-{number:03d}",
        run_id=f"run-{deployment}-{number:03d}",
        deployment=deployment,
        started_at=started,
        finished_at=started + timedelta(minutes=8),
        status="failed" if failed else "succeeded",
        actions=actions,
        provenance=Provenance(intent=intent, plan_storage="backend"),
        definitions=results,
    )


def _runs(now: datetime) -> List[Tuple[str, RunRecord]]:
    """(store, record) pairs, oldest first."""
    day, hour = timedelta(days=1), timedelta(hours=1)
    small, big = ChangeSummary(add=1, change=2), ChangeSummary(add=4, destroy=2)
    runs: List[Tuple[str, RunRecord]] = []
    # a week of healthy daily deploys everywhere in production
    for days_ago in range(7, 0, -1):
        at = now - days_ago * day
        for deployment in ("payments", "search", "edge"):
            runs.append(
                (
                    "demo-prod",
                    _record(
                        deployment,
                        8 - days_ago,
                        at,
                        [
                            _result(
                                deployment,
                                definition,
                                [
                                    _phase("plan", "succeeded", at),
                                    _phase("apply", "succeeded", at + hour / 6),
                                ],
                                changes=small,
                            )
                            for definition in ("network", "database", "service")
                        ],
                    ),
                )
            )
    # payments/database fails its newest apply: failing
    at = now - 2 * hour
    runs.append(
        (
            "demo-prod",
            _record(
                "payments",
                9,
                at,
                [
                    _result(
                        "payments",
                        "database",
                        [
                            _phase("plan", "succeeded", at),
                            _phase("apply", "failed", at + hour / 6, "exit status 1"),
                        ],
                        ref="v1.5.0",
                        changes=big,
                    )
                ],
            ),
        )
    )
    # search/service drifted since its last apply: drifted
    at = now - 5 * hour
    runs.append(
        (
            "demo-prod",
            _record(
                "search",
                9,
                at,
                [
                    _result(
                        "search",
                        "service",
                        [_phase("plan", "succeeded", at)],
                        changes=ChangeSummary(change=1),
                    )
                ],
                intent="drift",
            ),
        )
    )
    # edge/network planned and saved for approval: pending (see _plans)
    at = now - 3 * hour
    runs.append(
        (
            "demo-prod",
            _record(
                "edge",
                9,
                at,
                [
                    _result(
                        "edge",
                        "network",
                        [
                            _phase("plan", "succeeded", at),
                            _phase("apply", "skipped", at, "saved for approval"),
                        ],
                        ref="v1.5.0",
                        changes=big,
                    )
                ],
            ),
        )
    )
    # edge/cdn was planned and saved more than a day ago and nobody has
    # approved it: pending, and over Production's one-day TTL (see _plans)
    at = now - 30 * hour
    runs.append(
        (
            "demo-prod",
            _record(
                "edge",
                10,
                at,
                [
                    _result(
                        "edge",
                        "cdn",
                        [
                            _phase("plan", "succeeded", at),
                            _phase("apply", "skipped", at, "saved for approval"),
                        ],
                        changes=small,
                    )
                ],
            ),
        )
    )
    # sandboxes: one on a newer module, one applied from a local checkout
    for deployment, offset, local, ref in (
        ("sandbox-ana", 26, False, "v1.6.0-rc1"),
        ("sandbox-raj", 4, True, "v1.4.0"),
    ):
        at = now - offset * hour
        runs.append(
            (
                "demo-dev",
                _record(
                    deployment,
                    1,
                    at,
                    [
                        _result(
                            deployment,
                            definition,
                            [
                                _phase("plan", "succeeded", at),
                                _phase("apply", "succeeded", at + hour / 6),
                            ],
                            ref=ref,
                            changes=small,
                            local=local,
                        )
                        for definition in ("network", "service")
                    ],
                ),
            )
        )
    return runs


def _plan(
    store: str,
    deployment: str,
    definition: str,
    number: int,
    at: datetime,
    storage: str = "backend",
    status: Optional[str] = None,
) -> db.Plan:
    plan_id = f"{deployment}-{definition}-{number}"
    body = PlanManifest(
        plan_id=plan_id,
        run_id=f"run-{deployment}-{number:03d}",
        deployment=deployment,
        definition=definition,
        storage=storage,
        plan_key=f"terraform/state/{deployment}/{definition}/{plan_id}.tfplan",
        sha256="0" * 64,
        size=48213,
        changes=ChangeSummary(add=4, destroy=2),
        created_at=at,
        expires_at=at + timedelta(days=2),
    )
    if status == "applied":
        body = body.moved("applying", at=at + timedelta(hours=1))
    if status:
        body = body.moved(status, at=at + timedelta(hours=1), reason="demo")
    return db.Plan(
        store=store,
        plan_id=plan_id,
        deployment=deployment,
        definition=definition,
        status=body.status,
        created_at=at,
        body=body.model_dump(mode="json", by_alias=True),
    )


def _lock(store, deployment, definition, who, created, observed, beat=None):
    host = who.split("@")[1]
    lease = None
    if beat is not None:
        lease = {
            "definition": definition,
            "action": "apply",
            "who": who,
            "host": host,
            "pid": 4242,
            "started_at": created.isoformat(),
            "heartbeat_at": beat.isoformat(),
            "heartbeat_seconds": 30,
        }
    return db.Lock(
        store=store,
        deployment=deployment,
        definition=definition,
        lock_id=f"{deployment}-{definition}-lock",
        who=who,
        operation="OperationTypeApply",
        created=created,
        lease=lease,
        observed_at=observed,
    )


def seed(session: Session, now: datetime) -> bool:
    """
    Seed the demo into an empty database. Returns False when the demo is
    already there, and raises DemoRefused when other data is.
    """
    if is_seeded(session):
        return False
    hour, minute = timedelta(hours=1), timedelta(minutes=1)
    for name, region in STORES.items():
        session.add(
            db.Store(
                name=name,
                region=region,
                bucket=f"{name}-terraform-state",
                prefix="terraform/state/{deployment}",
                credentials=MARKER,
                reconciled_at=now,
            )
        )
    runs = _runs(now)
    deployments: Dict[Tuple[str, str], set] = {}
    for store, run in runs:
        for item in run.definitions:
            deployments.setdefault((store, run.deployment), set()).add(item.definition)
    for (store, name), definitions in sorted(deployments.items()):
        session.add(
            db.Deployment(
                store=store,
                name=name,
                prefix=f"terraform/state/{name}",
                record_name=None,
                discovered_from="template",
                first_seen=now - 30 * 24 * hour,
                last_seen=now,
                group_name=GROUPS.of(store, name),
            )
        )
    # deployments first: without relationships the unit of work does not order
    # these inserts, and Postgres enforces the foreign key at once
    session.flush()
    for (store, name), definitions in sorted(deployments.items()):
        for definition in sorted(definitions):
            session.add(
                db.Definition(
                    store=store,
                    deployment=name,
                    name=definition,
                    state_key=f"terraform/state/{name}/{definition}/terraform.tfstate",
                    last_seen=now,
                )
            )
    session.flush()
    reconciler = Reconciler([], None, now=lambda: now)
    statuses: Dict[Tuple[str, str, str], object] = {}
    for store, run in runs:
        reconciler._project(
            session, store, run.deployment, "run", run.model_dump_json(by_alias=True)
        )
        for item in run.definitions:
            key = (store, run.deployment, item.definition)
            statuses[key] = merge_status(statuses.get(key), run, item, now)
    for (store, deployment, definition), status in statuses.items():
        session.add(
            db.Status(
                store=store,
                deployment=deployment,
                definition=definition,
                updated_at=status.updated_at,
                body=status.model_dump(mode="json", by_alias=True),
            )
        )
    session.add_all(
        [
            # awaiting approval, and over Production's one-day TTL
            _plan("demo-prod", "edge", "network", 9, now - 3 * hour),
            _plan("demo-prod", "edge", "cdn", 10, now - 30 * hour),
            # a preview and plans that have moved on
            _plan("demo-prod", "search", "service", 9, now - 5 * hour, "preview"),
            _plan("demo-prod", "edge", "network", 7, now - 50 * hour, status="applied"),
            _plan(
                "demo-dev",
                "sandbox-ana",
                "service",
                1,
                now - 26 * hour,
                status="discarded",
            ),
            # live: the worker's lease is fresh
            _lock(
                "demo-prod",
                "search",
                "database",
                "runner@worker-7f9c",
                now - 6 * minute,
                now,
                beat=now - 20 * timedelta(seconds=1),
            ),
            # stale: held from a laptop hours ago, with no lease
            _lock(
                "demo-dev", "sandbox-raj", "network", "raj@laptop", now - 4 * hour, now
            ),
        ]
    )
    session.commit()
    return True
