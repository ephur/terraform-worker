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

State bodies are never read. The deployment's name comes from its run
records when it has any, and otherwise from the prefix segment.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker
from tfworker.backends.lock import LockInfo, WorkerLease
from tfworker.report import DefinitionStatus, PlanManifest, RunRecord

from . import db
from .stores import StoreConfig, list_keys, list_segments

log = logging.getLogger(__name__)

STATE_FILE = "terraform.tfstate"
LOCK_SUFFIX = ".tflock"
LEASE_SUFFIX = ".tfworker-lease."
REPORT_DIR = "_tfworker/"
KINDS = {"runs": "run", "status": "status", "plans": "plan"}


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
    ):
        self.stores = {store.name: store for store in stores}
        self.sessions = sessions
        self.now = now

    def reconcile_all(self) -> List[StoreSummary]:
        return [self.reconcile(name) for name in self.stores]

    def reconcile(self, name: str) -> StoreSummary:
        store = self.stores[name]
        summary = StoreSummary(store=name)
        with self.sessions() as session:
            row = self._store_row(session, store)
            try:
                client = store.session().client("s3")
                for segment in sorted(
                    set(list_segments(client, store.bucket, store.listing_prefix))
                ):
                    self._deployment(session, client, store, segment, summary)
                row.last_error = "; ".join(summary.errors)[:2048] or None
            except Exception as e:  # the next store still reconciles
                log.error(f"unable to reconcile store {name}: {e}")
                row.last_error = str(e)[:2048]
                summary.errors.append(str(e))
            row.reconciled_at = self.now()
            session.commit()
        return summary

    def rebuild(self) -> List[StoreSummary]:
        """Empty the projection and reconcile every store from scratch."""
        with self.sessions() as session:
            for table in reversed(db.Base.metadata.sorted_tables):
                session.execute(delete(table))
            session.commit()
        return self.reconcile_all()

    def _store_row(self, session: Session, store: StoreConfig) -> db.Store:
        return session.merge(
            db.Store(
                name=store.name,
                region=store.region,
                bucket=store.bucket,
                prefix=store.prefix,
                credentials=store.credentials.kind,
            )
        )

    def _deployment(
        self,
        session: Session,
        client,
        store: StoreConfig,
        segment: str,
        summary: StoreSummary,
    ) -> None:
        prefix = store.render(segment)
        listing = classify(prefix, list_keys(client, store.bucket, prefix + "/"))
        if not (listing.reports or listing.states or listing.locks):
            return
        now = self.now()
        name, source = self._deployment_name(session, client, store, listing, segment)
        existing = session.get(db.Deployment, (store.name, name))
        session.merge(
            db.Deployment(
                store=store.name,
                name=name,
                prefix=prefix,
                discovered_from=source,
                first_seen=existing.first_seen if existing else now,
                last_seen=now,
            )
        )
        session.flush()
        summary.deployments += 1

        self._reports(session, client, store, name, prefix, listing, summary)
        for definition, key in listing.states.items():
            self._definition(session, store.name, name, definition, key, now)
        self._locks(session, client, store, name, listing, summary)

    def _deployment_name(
        self, session, client, store, listing: _Listing, segment: str
    ) -> Tuple[str, str]:
        """The deployment named by its run records, else the prefix segment."""
        runs = sorted(
            key for key, (kind, _) in listing.reports.items() if kind == "run"
        )
        if runs:
            seen = session.get(db.SeenObject, (store.name, runs[-1]))
            if seen is not None:
                return seen.deployment, (
                    "record" if seen.deployment != segment else "template"
                )
            try:
                record = RunRecord.model_validate_json(
                    _get(client, store.bucket, runs[-1])
                )
                if record.deployment != segment:
                    return record.deployment, "record"
            except Exception as e:
                log.warning(f"unable to read {runs[-1]} for its deployment: {e}")
        return segment, "template"

    def _reports(self, session, client, store, deployment, prefix, listing, summary):
        seen = {
            row.key: row
            for row in session.scalars(
                select(db.SeenObject).where(
                    db.SeenObject.store == store.name,
                    db.SeenObject.key.startswith(prefix + "/" + REPORT_DIR),
                )
            )
        }
        for key, (kind, etag) in sorted(listing.reports.items()):
            previous = seen.get(key)
            if previous is not None and previous.etag == etag:
                summary.skipped += 1
                continue
            try:
                ref = self._project(
                    session,
                    store.name,
                    deployment,
                    kind,
                    _get(client, store.bucket, key),
                )
            except (ValidationError, ValueError) as e:
                summary.errors.append(f"{key}: {e.__class__.__name__}")
                log.warning(f"unable to project {key}: {e}")
                continue
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
            summary.fetched += 1
        for key, row in seen.items():
            if key not in listing.reports:
                self._remove(session, row)
                summary.removed += 1

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
            for result in record.definitions:
                self._definition(
                    session, store, deployment, result.definition, None, now
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
