"""
Project what the worker writes beside each run's state into the console's database.

For each store the reconciler finds deployments by listing the prefix
template, lists each deployment's prefix once, and:

- fetches run records, status snapshots and plan manifests under
  ``_tfworker/`` whose ETag changed since the last reconcile, and removes
  rows whose object is gone
- records each definition that has a state object, without reading it
- reads every ``.tflock`` object, and its holder's worker lease, so the
  lock table shows what is held now

State bodies are never read. A deployment is identified by its prefix
segment, which never changes; the name its newest run record gives is kept
beside it as ``record_name``. Deployments whose prefix is gone or empty,
definitions whose state object is gone, and stores no longer configured leave
the projection.

Each store reconciles in one transaction. Each deployment and each object is
projected in a savepoint, so one that cannot be read or stored is reported
and the rest still land.
"""

import logging
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import quote

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker
from tfworker.backends.lock import LockInfo, WorkerLease
from tfworker.report import DefinitionStatus, PlanManifest, RunRecord
from tfworker.report.models import LOCAL_MODULE_TAG

from . import db
from .groups import Groups
from .stores import StoreConfig, list_keys, list_segments

log = logging.getLogger(__name__)

STATE_FILE = "terraform.tfstate"
LOCK_SUFFIX = ".tflock"
LEASE_SUFFIX = ".tfworker-lease."
REPORT_DIR = "_tfworker/"
KINDS = {"runs": "run", "status": "status", "plans": "plan"}
# every table keyed by store and deployment, children before parents
PROJECTED = (
    db.Definition,
    db.Lock,
    db.RunDefinition,
    db.Run,
    db.Status,
    db.Plan,
    db.SeenObject,
)


@dataclass
class StoreSummary:
    store: str
    deployments: int = 0
    fetched: int = 0
    skipped: int = 0
    removed: int = 0
    locks: int = 0
    errors: List[str] = field(default_factory=list)


@dataclass
class _Listing:
    """One deployment prefix, sorted into what the reconciler reads."""

    reports: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    states: Dict[str, str] = field(default_factory=dict)
    locks: Dict[str, str] = field(default_factory=dict)
    leases: Dict[str, str] = field(default_factory=dict)


def classify(prefix: str, keys: Iterable[Tuple[str, str]]) -> _Listing:
    listing = _Listing()
    base = prefix + "/"
    for key, etag in keys:
        rest = key[len(base) :]
        if rest.startswith(REPORT_DIR):
            kind = KINDS.get(rest[len(REPORT_DIR) :].split("/", 1)[0])
            if kind and key.endswith(".json"):
                listing.reports[key] = (kind, etag)
            continue
        parts = rest.split("/")
        if len(parts) != 2:
            continue
        definition, name = parts
        if name == STATE_FILE:
            listing.states[definition] = key
        elif name == STATE_FILE + LOCK_SUFFIX:
            listing.locks[definition] = key
        elif name.startswith(STATE_FILE + LEASE_SUFFIX):
            listing.leases[key] = etag
    return listing


class Reconciler:
    def __init__(
        self,
        stores: List[StoreConfig],
        sessions: sessionmaker,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        groups: Optional[Groups] = None,
    ):
        self.stores = {store.name: store for store in stores}
        self.groups = groups or Groups()
        self.sessions = sessions
        self.now = now
        # told of each store's summary and how many seconds it took, such as
        # by the metrics; an observer's error never reaches the reconcile
        self.observers: List[Callable[[StoreSummary, float], None]] = []
        # one reconcile or event of a store at a time in this process; across
        # replicas the store's row lock does the same
        self._turns: Dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._turns_lock = threading.Lock()

    def reconcile_all(self) -> List[StoreSummary]:
        self._forget_unconfigured()
        summaries = []
        for name in self.stores:
            try:
                summaries.append(self.reconcile(name))
            except Exception as e:  # the next store still reconciles
                log.exception(f"unable to record the reconcile of store {name}: {e}")
                summaries.append(StoreSummary(store=name, errors=[str(e)]))
        return summaries

    @contextmanager
    def turn(self, name: str, wait: bool = True) -> Iterator[bool]:
        """
        Hold this process's turn at a store, saying whether it was had.

        SQLite ignores row locks, so this is what keeps the schedule and the
        event consumer of one console from writing a store at once.
        """
        with self._turns_lock:
            lock = self._turns[name]
        had = lock.acquire(blocking=wait)
        try:
            yield had
        finally:
            if had:
                lock.release()

    def reconcile(self, name: str) -> StoreSummary:
        """
        Reconcile one store in one transaction.

        The store's row is locked for the transaction, so two reconcilers of
        the same store (the schedule and the CLI, or two replicas) take turns.
        """
        with self.turn(name):
            started = time.monotonic()
            try:
                summary = self._reconcile(name)
            except Exception as e:
                self._observe(StoreSummary(store=name, errors=[str(e)]), started)
                raise
        self._observe(summary, started)
        return summary

    def _observe(self, summary: StoreSummary, started: float) -> None:
        seconds = time.monotonic() - started
        for observer in self.observers:
            try:
                observer(summary, seconds)
            except Exception as e:
                log.warning(f"a reconcile observer failed: {e}", exc_info=True)

    def _reconcile(self, name: str) -> StoreSummary:
        store = self.stores[name]
        summary = StoreSummary(store=name)
        with self.sessions() as session:
            session.merge(self._store_row(store))
            session.commit()
            row = session.execute(
                select(db.Store)
                .where(db.Store.name == name)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one()
            try:
                # events written before this are seen by this reconcile
                row.reconcile_started_at = self.now()
                self._store(session, store, summary)
                row.last_error = "; ".join(summary.errors)[:2048] or None
                row.reconciled_at = self.now()
                session.commit()
            except Exception as e:  # the next store still reconciles
                log.exception(f"unable to reconcile store {name}: {e}")
                session.rollback()
                error = f"{e.__class__.__name__}: {e}"
                summary.errors.append(error)
                row = session.get(db.Store, name)
                row.last_error = error[:2048]
                row.reconciled_at = self.now()
                session.commit()
        return summary

    def rebuild(self) -> List[StoreSummary]:
        """Empty the projection and reconcile every store from scratch."""
        with self.sessions() as session:
            for table in reversed(db.Base.metadata.sorted_tables):
                if table.name in db.PROJECTION:
                    session.execute(delete(table))
            session.commit()
        return self.reconcile_all()

    @staticmethod
    def _store_row(store: StoreConfig) -> db.Store:
        return db.Store(
            name=store.name,
            region=store.region,
            bucket=store.bucket,
            prefix=store.prefix,
            credentials=store.credentials.kind,
        )

    def _forget_unconfigured(self) -> None:
        """Drop every row of a store that is no longer in the stores file."""
        with self.sessions() as session:
            gone = session.scalars(
                select(db.Store.name).where(db.Store.name.not_in(list(self.stores)))
            ).all()
            for name in gone:
                log.info(f"store {name} is no longer configured, forgetting it")
                for model in PROJECTED + (db.Deployment,):
                    session.execute(delete(model).where(model.store == name))
                session.execute(delete(db.Store).where(db.Store.name == name))
            session.commit()

    def _store(self, session: Session, store: StoreConfig, summary) -> None:
        client = store.session().client("s3")
        segments = sorted(
            set(list_segments(client, store.bucket, store.listing_prefix))
        )
        kept = set()
        for segment in segments:
            try:
                with session.begin_nested():
                    if self._deployment(session, client, store, segment, summary):
                        kept.add(segment)
            except Exception as e:  # keep what the last reconcile projected
                kept.add(segment)
                summary.errors.append(
                    f"{store.render(segment)}: {e.__class__.__name__}"
                )
                log.warning(f"unable to reconcile {store.render(segment)}: {e}")
        for name in session.scalars(
            select(db.Deployment.name).where(
                db.Deployment.store == store.name, db.Deployment.name.not_in(kept)
            )
        ).all():
            log.info(f"deployment {name} in store {store.name} is gone")
            self._forget_deployment(session, store.name, name)

    @staticmethod
    def _forget_deployment(session: Session, store: str, name: str) -> None:
        for model in PROJECTED:
            session.execute(
                delete(model).where(model.store == store, model.deployment == name)
            )
        session.execute(
            delete(db.Deployment).where(
                db.Deployment.store == store, db.Deployment.name == name
            )
        )

    def _deployment(
        self,
        session: Session,
        client,
        store: StoreConfig,
        segment: str,
        summary: StoreSummary,
    ) -> bool:
        """Project one deployment, and say whether it still has anything in it."""
        prefix = store.render(segment)
        listing = classify(prefix, list_keys(client, store.bucket, prefix + "/"))
        if not (listing.reports or listing.states or listing.locks):
            return False
        now = self.now()
        row = session.get(db.Deployment, (store.name, segment))
        if row is None:
            row = db.Deployment(
                store=store.name,
                name=segment,
                prefix=prefix,
                discovered_from="template",
                first_seen=now,
                last_seen=now,
            )
            session.add(row)
            session.flush()
        row.prefix = prefix
        row.last_seen = now
        row.group_name = self.groups.of(store.name, segment)
        summary.deployments += 1

        self._reports(session, client, store, segment, listing, summary)
        self._states(session, store.name, segment, listing, now)
        self._locks(session, client, store, segment, listing, summary)
        row.record_name = self._record_name(session, store.name, segment)
        row.discovered_from = "record" if row.record_name else "template"
        return True

    @staticmethod
    def _record_name(session: Session, store: str, segment: str) -> Optional[str]:
        """The name the newest run record gives, when it is not the segment."""
        body = session.scalar(
            select(db.Run.body)
            .where(db.Run.store == store, db.Run.deployment == segment)
            .order_by(db.Run.started_at.desc(), db.Run.record_id.desc())
            .limit(1)
        )
        name = body.get("deployment") if body else None
        return name if name and name != segment else None

    def _reports(self, session, client, store, deployment, listing, summary):
        seen = {
            row.key: row
            for row in session.scalars(
                select(db.SeenObject).where(
                    db.SeenObject.store == store.name,
                    db.SeenObject.deployment == deployment,
                )
            )
        }
        for key, (kind, etag) in sorted(listing.reports.items()):
            previous = seen.get(key)
            if previous is not None and previous.etag == etag:
                summary.skipped += 1
                continue
            try:
                text = _get(client, store.bucket, key)
                with session.begin_nested():
                    ref = self._project(session, store.name, deployment, kind, text)
                    session.merge(
                        db.SeenObject(
                            store=store.name,
                            key=key,
                            etag=etag,
                            kind=kind,
                            deployment=deployment,
                            ref=ref,
                        )
                    )
            except Exception as e:  # one bad object never stops the rest
                summary.errors.append(f"{key}: {e.__class__.__name__}")
                log.warning(f"unable to project {key}: {e}")
                continue
            summary.fetched += 1
        for key, row in seen.items():
            if key not in listing.reports:
                self._remove(session, row)
                summary.removed += 1

    def _states(self, session, store, deployment, listing, now) -> None:
        for definition, key in listing.states.items():
            self._definition(session, store, deployment, definition, key, now)
        for row in session.scalars(
            select(db.Definition).where(
                db.Definition.store == store,
                db.Definition.deployment == deployment,
                db.Definition.state_key.is_not(None),
            )
        ):
            if row.name not in listing.states:
                row.state_key = None

    def _project(
        self, session, store: str, deployment: str, kind: str, text: str
    ) -> str:
        now = self.now()
        if kind == "run":
            record = RunRecord.model_validate_json(text)
            session.merge(
                db.Run(
                    store=store,
                    record_id=record.record_id,
                    run_id=record.run_id,
                    deployment=deployment,
                    status=record.status,
                    started_at=record.started_at,
                    finished_at=record.finished_at,
                    body=record.model_dump(mode="json", by_alias=True),
                )
            )
            session.execute(
                delete(db.RunDefinition).where(
                    db.RunDefinition.store == store,
                    db.RunDefinition.record_id == record.record_id,
                )
            )
            for result in record.definitions:
                self._definition(
                    session, store, deployment, result.definition, None, now
                )
                session.merge(
                    db.RunDefinition(
                        store=store,
                        record_id=record.record_id,
                        definition=result.definition,
                        deployment=deployment,
                        started_at=record.started_at,
                        phases={
                            action: phase.model_dump(mode="json")
                            for action, phase in result.phases.items()
                        },
                        module_sources=[
                            source.model_dump(mode="json")
                            for source in result.module_sources
                        ],
                        tags=list(result.tags),
                        local_modules=LOCAL_MODULE_TAG in result.tags,
                    )
                )
            return record.record_id
        if kind == "status":
            status = DefinitionStatus.model_validate_json(text)
            session.merge(
                db.Status(
                    store=store,
                    deployment=deployment,
                    definition=status.definition,
                    updated_at=status.updated_at,
                    body=status.model_dump(mode="json", by_alias=True),
                )
            )
            self._definition(session, store, deployment, status.definition, None, now)
            return status.definition
        manifest = PlanManifest.model_validate_json(text)
        session.merge(
            db.Plan(
                store=store,
                plan_id=manifest.plan_id,
                deployment=deployment,
                definition=manifest.definition,
                status=manifest.status,
                created_at=manifest.created_at,
                body=manifest.model_dump(mode="json", by_alias=True),
            )
        )
        return manifest.plan_id

    def _remove(self, session: Session, seen: db.SeenObject) -> None:
        model, key = {
            "run": (db.Run, (seen.store, seen.ref)),
            "status": (db.Status, (seen.store, seen.deployment, seen.ref)),
            "plan": (db.Plan, (seen.store, seen.ref)),
        }[seen.kind]
        if seen.kind == "run":
            session.execute(
                delete(db.RunDefinition).where(
                    db.RunDefinition.store == seen.store,
                    db.RunDefinition.record_id == seen.ref,
                )
            )
        row = session.get(model, key)
        if row is not None:
            session.delete(row)
        session.delete(seen)

    def _definition(self, session, store, deployment, name, state_key, now) -> None:
        existing = session.get(db.Definition, (store, deployment, name))
        if existing is not None:
            existing.last_seen = now
            if state_key:
                existing.state_key = state_key
            return
        session.add(
            db.Definition(
                store=store,
                deployment=deployment,
                name=name,
                state_key=state_key,
                last_seen=now,
            )
        )
        session.flush()

    def _locks(self, session, client, store, deployment, listing, summary) -> None:
        held = set()
        for definition, key in sorted(listing.locks.items()):
            try:
                lock = LockInfo.model_validate_json(_get(client, store.bucket, key))
            except Exception as e:
                summary.errors.append(f"{key}: {e.__class__.__name__}")
                log.warning(f"unable to read lock {key}: {e}")
                continue
            held.add(definition)
            session.merge(
                db.Lock(
                    store=store.name,
                    deployment=deployment,
                    definition=definition,
                    lock_id=lock.id,
                    who=lock.who,
                    operation=lock.operation,
                    created=lock.created,
                    lease=self._lease(client, store, key, lock, listing),
                    observed_at=self.now(),
                )
            )
            summary.locks += 1
        session.execute(
            delete(db.Lock).where(
                db.Lock.store == store.name,
                db.Lock.deployment == deployment,
                db.Lock.definition.not_in(held),
            )
        )

    @staticmethod
    def _lease(client, store, lock_key, lock: LockInfo, listing) -> Optional[dict]:
        if not lock.host:
            return None
        state_key = lock_key[: -len(LOCK_SUFFIX)]
        key = f"{state_key}{LEASE_SUFFIX}{quote(lock.host, safe='')}"
        if key not in listing.leases:
            return None
        try:
            lease = WorkerLease.model_validate_json(_get(client, store.bucket, key))
        except Exception as e:
            log.warning(f"unable to read lease {key}: {e}")
            return None
        return lease.model_dump(mode="json")


def _get(client, bucket: str, key: str) -> str:
    return client.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
