"""
Apply the worker's CloudEvents to the projection as they arrive.

The worker's ``sqs`` handler, in ``cloudevents`` format, sends each run
record, status snapshot, plan manifest and lock wait as it is written (see
``tfworker.report.events``). When a queue is configured, the console reads it
and applies each event straight away, so screens follow runs without waiting
for the next reconcile.

The reconciler stays the source of truth. An event only ever writes what the
reconciler would have read from the bucket, and it marks the object it came
from as not yet read, so the next reconcile reads it again and removes the
row if the object is gone. Events are applied idempotently: a duplicate, or
one older than what is already projected, changes nothing. An event the
console cannot place in a configured store is dropped, and the reconciler
picks the object up on its own.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session, sessionmaker
from tfworker.backends.lock import LockInfo
from tfworker.report import DefinitionStatus, PlanManifest, RunRecord
from tfworker.report.events import (
    LOCK_WAITING_EVENT,
    PLAN_EVENT,
    RUN_EVENT,
    STATUS_EVENT,
)
from tfworker.report.models import BackendLocation

from . import db
from .groups import Groups
from .reconcile import REPORT_DIR, Reconciler
from .stores import Credentials, StoreConfig

log = logging.getLogger(__name__)

# what an event did; the consumer logs it
APPLIED = "applied"
UNCHANGED = "unchanged"
IGNORED = "ignored"
# what became of a message that did not apply
DROPPED = "dropped"
BUSY = "busy"
FAILED = "failed"


class InvalidEvent(ValueError):
    """An event that can never be applied; it is dropped rather than retried."""


class StoreBusy(RuntimeError):
    """The event's store is reconciling; the event is tried again shortly."""

    def __init__(self, store: str):
        super().__init__(f"store {store} is reconciling")
        self.store = store


@dataclass(frozen=True)
class Target:
    store: StoreConfig
    segment: str


class EventIngest:
    """Applies one CloudEvent at a time to the projection."""

    def __init__(
        self,
        reconciler: Reconciler,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.reconciler = reconciler
        self.now = now

    @property
    def stores(self) -> Dict[str, StoreConfig]:
        return self.reconciler.stores

    @property
    def sessions(self) -> sessionmaker:
        return self.reconciler.sessions

    @property
    def groups(self) -> Groups:
        return self.reconciler.groups

    def apply(self, event: Dict[str, Any]) -> str:
        """
        Apply one event and say what it did.

        Raises InvalidEvent for an event that can never apply, and StoreBusy
        while its store reconciles. Any other error, such as the database
        being unavailable, is left to the caller to retry.
        """
        kind = event.get("type")
        handler = (
            {
                RUN_EVENT: (RunRecord, self._run),
                STATUS_EVENT: (DefinitionStatus, self._status),
                PLAN_EVENT: (PlanManifest, self._plan),
                LOCK_WAITING_EVENT: (None, self._lock),
            }.get(kind)
            if isinstance(kind, str)
            else None
        )
        if handler is None:
            return IGNORED
        model, apply = handler
        data = event.get("data")
        if not isinstance(data, dict):
            raise InvalidEvent(f"{kind} event has no data object")
        try:
            if model is None:
                body = _LockWait.parse(data, _event_time(event))
            else:
                body = model.model_validate(data)
            at = _written_at(body)
        except (ValidationError, ValueError, TypeError, OverflowError) as e:
            raise InvalidEvent(f"unreadable {kind} event: {e}") from e
        if isinstance(body, PlanManifest) and body.storage == "local":
            # only the run's own machine has it; the console cannot read it
            return IGNORED
        with self.sessions() as session:
            target = self._target(session, body)
            if target is None:
                log.debug(f"no configured store holds the {kind} event's deployment")
                return IGNORED
            with self.reconciler.turn(target.store.name, wait=False) as turn:
                if not turn:
                    raise StoreBusy(target.store.name)
                row = self._lock_store(session, target.store)
                if row is None:
                    raise StoreBusy(target.store.name)
                started = row.reconcile_started_at
                if started is not None and at < started:
                    # written before the last reconcile listed the store, so
                    # that reconcile saw it, or saw it was already gone
                    return UNCHANGED
                try:
                    outcome = apply(session, target, body, at)
                    session.commit()
                except DataError as e:
                    # too long for a column, or a NUL Postgres refuses
                    raise InvalidEvent(f"{kind} event does not fit: {e.orig}") from e
        return outcome

    # --- placing an event ---------------------------------------------------

    def _target(self, session: Session, body) -> Optional[Target]:
        for location in _locations(body):
            target = self._by_location(location)
            if target is not None:
                return target
        return self._by_name(session, body.deployment)

    def _by_location(self, location: Tuple[Optional[str], str]) -> Optional[Target]:
        bucket, key = location
        matches = []
        for store in self.stores.values():
            if bucket is not None and store.bucket != bucket:
                continue
            segment = store.segment_of(key)
            if segment is not None:
                matches.append(Target(store, segment))
        if len(matches) != 1:
            return None
        return matches[0]

    def _by_name(self, session: Session, name: str) -> Optional[Target]:
        """The one projected deployment that the records call this name."""
        rows = session.execute(
            select(db.Deployment.store, db.Deployment.name).where(
                db.Deployment.store.in_(list(self.stores)),
                or_(
                    db.Deployment.record_name == name,
                    (db.Deployment.name == name) & db.Deployment.record_name.is_(None),
                ),
            )
        ).all()
        if len(rows) != 1:
            return None
        store, segment = rows[0]
        return Target(self.stores[store], segment)

    def _lock_store(self, session: Session, store: StoreConfig) -> Optional[db.Store]:
        """
        The store's row, locked as a reconcile locks it; None while another
        replica reconciles the store, rather than waiting it out.
        """
        if session.get(db.Store, store.name) is None:
            session.merge(Reconciler._store_row(store))
            session.commit()
        return session.scalar(
            select(db.Store)
            .where(db.Store.name == store.name)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )

    def _deployment(self, session: Session, target: Target) -> db.Deployment:
        row = session.get(db.Deployment, (target.store.name, target.segment))
        if row is not None:
            return row
        now = self.now()
        row = db.Deployment(
            store=target.store.name,
            name=target.segment,
            prefix=target.store.render(target.segment),
            discovered_from="template",
            group_name=self.groups.of(target.store.name, target.segment),
            first_seen=now,
            last_seen=now,
        )
        session.add(row)
        session.flush()
        return row

    # --- applying each type -------------------------------------------------

    def _run(self, session: Session, target: Target, record: RunRecord, at) -> str:
        store = target.store.name
        if session.get(db.Run, (store, record.record_id)) is not None:
            return UNCHANGED  # run records never change once written
        day = record.started_at.strftime("%Y/%m/%d")
        self._project(
            session, target, "run", f"runs/{day}/{record.record_id}.json", record
        )
        row = self._deployment(session, target)
        row.record_name = Reconciler._record_name(session, store, target.segment)
        row.discovered_from = "record" if row.record_name else "template"
        return APPLIED

    def _status(self, session, target: Target, status: DefinitionStatus, at) -> str:
        current = session.get(
            db.Status, (target.store.name, target.segment, status.definition)
        )
        if current is not None and current.updated_at >= status.updated_at:
            return UNCHANGED
        self._project(
            session, target, "status", f"status/{status.definition}.json", status
        )
        return APPLIED

    def _plan(self, session, target: Target, manifest: PlanManifest, at) -> str:
        current = session.get(db.Plan, (target.store.name, manifest.plan_id))
        # a manifest only grows: every status change adds to its history
        if current is not None and len(current.body.get("history", [])) >= len(
            manifest.history
        ):
            return UNCHANGED
        path = f"plans/{manifest.definition}/{manifest.plan_id}.json"
        self._project(session, target, "plan", path, manifest)
        return APPLIED

    def _lock(self, session, target: Target, wait: "_LockWait", at) -> str:
        store, segment = target.store.name, target.segment
        lock = wait.lock
        current = session.get(db.Lock, (store, segment, wait.definition))
        if current is not None and (
            current.observed_at >= at or current.lock_id == lock.id
        ):
            # seen since, or the same lock: keep the lease and when it was
            # read together, since the screens judge one by the other
            return UNCHANGED
        self._deployment(session, target)
        session.merge(
            db.Lock(
                store=store,
                deployment=segment,
                definition=wait.definition,
                lock_id=lock.id,
                who=lock.who,
                operation=lock.operation,
                created=lock.created,
                # the next reconcile reads the holder's lease
                lease=None,
                observed_at=at,
            )
        )
        return APPLIED

    def _project(self, session, target: Target, kind: str, path: str, body) -> None:
        store = target.store.name
        self._deployment(session, target)
        text = body.model_dump_json(by_alias=True)
        ref = self.reconciler._project(session, store, target.segment, kind, text)
        key = f"{target.store.render(target.segment)}/{REPORT_DIR}{path}"
        if session.get(db.SeenObject, (store, key)) is None:
            # no ETag: the next reconcile reads the object, or drops the row
            # if the object is gone
            session.add(
                db.SeenObject(
                    store=store,
                    key=key,
                    etag="",
                    kind=kind,
                    deployment=target.segment,
                    ref=ref,
                )
            )


@dataclass
class _LockWait:
    deployment: str
    definition: str
    lock: LockInfo
    at: datetime

    @classmethod
    def parse(cls, data: Dict[str, Any], at: datetime) -> "_LockWait":
        try:
            return cls(
                deployment=str(data["deployment"]),
                definition=str(data["definition"]),
                lock=LockInfo.model_validate(data["lock"]),
                at=at,
            )
        except KeyError as e:
            raise ValueError(f"missing {e}") from e


def _written_at(body) -> datetime:
    """When the worker wrote what the event carries, by the worker's clock."""
    if isinstance(body, RunRecord):
        return body.finished_at or body.started_at
    if isinstance(body, DefinitionStatus):
        return body.updated_at
    if isinstance(body, PlanManifest):
        return body.history[-1].at if body.history else body.created_at
    return body.at


def _locations(body) -> List[Tuple[Optional[str], str]]:
    """(bucket, key) pairs that place an event's deployment, best first."""
    locations: List[Optional[BackendLocation]] = []
    if isinstance(body, RunRecord):
        locations = [result.state for result in body.definitions]
    elif isinstance(body, (DefinitionStatus, PlanManifest)):
        locations = [body.state]
    elif isinstance(body, _LockWait) and "/" in body.lock.path:
        # terraform's S3 lock path is bucket/key
        bucket, key = body.lock.path.split("/", 1)
        return [(bucket, key)]
    return [
        (location.bucket, location.state_key)
        for location in locations
        if location is not None and location.backend == "s3"
    ]


def _event_time(event: Dict[str, Any]) -> datetime:
    try:
        at = datetime.fromisoformat(str(event["time"]))
    except (KeyError, ValueError) as e:
        raise ValueError(f"event has no valid time: {e}") from e
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


def envelope(body: str) -> Dict[str, Any]:
    """
    The CloudEvent in an SQS message body.

    The sqs handler sends the envelope itself. A queue fed by an EventBridge
    rule receives EventBridge's own event with the envelope as its detail.
    """
    try:
        message = json.loads(body)
    except ValueError as e:
        raise InvalidEvent(f"message is not JSON: {e}") from e
    if isinstance(message, dict) and "specversion" in message:
        return message
    detail = message.get("detail") if isinstance(message, dict) else None
    if isinstance(detail, dict) and "specversion" in detail:
        return detail
    raise InvalidEvent("message is not a CloudEvent")


class EventConsumer:
    """
    Reads an SQS queue on a thread while the console serves.

    A message is deleted once it is applied, or once it proves it can never
    be. A message whose store is reconciling comes back after
    ``BUSY_SECONDS``. Any other failure leaves it on the queue to come back
    after its visibility timeout, so a redrive policy decides when to give up
    on it.
    """

    WAIT_SECONDS = 20
    BATCH = 10
    # how soon an event whose store is reconciling is tried again
    BUSY_SECONDS = 10
    # assumed-role credentials expire, so the client is rebuilt this often
    CLIENT_SECONDS = 900.0

    def __init__(
        self,
        ingest: EventIngest,
        queue_url: str,
        credentials: Optional[Credentials] = None,
        region: Optional[str] = None,
        client=None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ingest = ingest
        self.queue_url = queue_url
        self.region = region or _queue_region(queue_url)
        self.credentials = credentials or Credentials()
        self._client = client
        self._fixed_client = client is not None
        self._client_made = clock()
        self._clock = clock
        # told of each message's event type and outcome, such as by the metrics
        self.observers: List[Callable[[str, str], None]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def client(self):
        stale = self._clock() - self._client_made >= self.CLIENT_SECONDS
        if self._client is None or (stale and not self._fixed_client):
            self._client = self.credentials.session(self.region).client("sqs")
            self._client_made = self._clock()
        return self._client

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="tfworker-console-events", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        # the thread is a daemon: a long poll in flight need not hold shutdown
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def poll(self) -> int:
        """Receive and apply one batch; returns how many messages were deleted."""
        response = self.client().receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=self.BATCH,
            WaitTimeSeconds=self.WAIT_SECONDS,
        )
        done = []
        for message in response.get("Messages", []):
            finished = self._handle(message)
            if finished is True:
                done.append(message)
            elif finished is BUSY:
                self._later(message)
        if not done:
            return 0
        result = self.client().delete_message_batch(
            QueueUrl=self.queue_url,
            Entries=[
                {"Id": str(n), "ReceiptHandle": message["ReceiptHandle"]}
                for n, message in enumerate(done)
            ],
        )
        failed = result.get("Failed", [])
        for failure in failed:
            # it comes back and applies again, which changes nothing
            log.warning(f"unable to delete an event message: {failure}")
        return len(done) - len(failed)

    def _later(self, message: Dict[str, Any]) -> None:
        try:
            self.client().change_message_visibility(
                QueueUrl=self.queue_url,
                ReceiptHandle=message["ReceiptHandle"],
                VisibilityTimeout=self.BUSY_SECONDS,
            )
        except Exception as e:  # it comes back after the queue's own timeout
            log.debug(f"unable to bring an event message back sooner: {e}")

    def _handle(self, message: Dict[str, Any]):
        """Apply one message: True when finished with, BUSY, or False to retry."""
        mid = message.get("MessageId", "?")
        kind = "unknown"
        try:
            event = envelope(message.get("Body", ""))
            kind = str(event.get("type") or "unknown")
            outcome = self.ingest.apply(event)
        except InvalidEvent as e:
            log.warning(f"dropping event message {mid}: {e}")
            self._observe(kind, DROPPED)
            return True
        except StoreBusy as e:
            log.debug(f"event message {mid} waits: {e}")
            self._observe(kind, BUSY)
            return BUSY
        except Exception as e:  # left for redelivery
            log.warning(f"unable to apply event message {mid}, will retry: {e}")
            self._observe(kind, FAILED)
            return False
        log.debug(f"{outcome} {kind} event {event.get('id')}")
        self._observe(kind, outcome)
        return True

    def _observe(self, kind: str, outcome: str) -> None:
        for observer in self.observers:
            try:
                observer(kind, outcome)
            except Exception as e:
                log.warning(f"an event observer failed: {e}")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as e:  # the consumer must keep running
                log.exception(f"unable to read events from {self.queue_url}: {e}")
                if not self._fixed_client:
                    self._client = None  # expired credentials, for one
                if self._stop.wait(self.WAIT_SECONDS):
                    return


def _queue_region(queue_url: str) -> Optional[str]:
    """The region in an SQS queue URL such as https://sqs.us-east-1.amazonaws.com/..."""
    host = queue_url.split("://", 1)[-1].split("/", 1)[0]
    parts = host.split(".")
    if len(parts) >= 3 and parts[0] == "sqs":
        return parts[1]
    return None
