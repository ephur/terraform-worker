import json
from datetime import timedelta
from unittest import mock

import boto3
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import DataError
from test_reconcile import (  # noqa: F401
    NOW,
    aws,
    bucket,
    engine,
    manifest,
    put,
    record,
    sessions,
    status,
)
from tfworker.report import BackendLocation
from tfworker.report.events import (
    LOCK_WAITING_EVENT,
    PLAN_EVENT,
    RUN_EVENT,
    STATUS_EVENT,
    cloudevent,
)

from tfworker_console import db
from tfworker_console.app import create_app
from tfworker_console.ingest import (
    APPLIED,
    DROPPED,
    IGNORED,
    UNCHANGED,
    EventConsumer,
    EventIngest,
    InvalidEvent,
    StoreBusy,
    _queue_region,
    envelope,
)
from tfworker_console.reconcile import Reconciler
from tfworker_console.settings import ConsoleSettings
from tfworker_console.stores import Credentials, StoreConfig

PREFIX = "terraform/state/alpha"
PROD = StoreConfig(
    name="prod",
    region="us-east-1",
    bucket="prod-state",
    prefix="terraform/state/{deployment}",
)
DEV = StoreConfig(
    name="dev", region="us-east-1", bucket="dev-state", prefix="tf/{deployment}"
)


def located(body, bucket_name="prod-state"):
    """The body with its state locations in a bucket, as the worker writes them."""
    if hasattr(body, "definitions"):
        for result in body.definitions:
            result.state.bucket = bucket_name
    elif body.state is not None:
        body.state.bucket = bucket_name
    return body


def event(kind, body, at=NOW, deployment="alpha", subject=None):
    return cloudevent(kind, deployment, subject, body, "run-R1", time=at)


def lock_wait(lock_id="lock-1", definition="net", path=None):
    return {
        "deployment": "alpha",
        "definition": definition,
        "lock": {
            "ID": lock_id,
            "Operation": "OperationTypeApply",
            "Who": "runner@pod-1",
            "Created": (NOW - timedelta(minutes=5)).isoformat(),
            "Path": path or f"prod-state/{PREFIX}/{definition}/terraform.tfstate",
        },
        "wait_seconds": 60,
    }


@pytest.fixture
def reconciler(sessions):  # noqa: F811
    return Reconciler([PROD, DEV], sessions, now=lambda: NOW)


@pytest.fixture
def ingest(reconciler):
    return EventIngest(reconciler, now=lambda: NOW)


def rows(sessions, model, **where):  # noqa: F811
    with sessions() as session:
        query = select(model)
        for name, value in where.items():
            query = query.where(getattr(model, name) == value)
        return session.scalars(query).all()


class TestApply:
    def test_run_record(self, ingest, sessions):  # noqa: F811
        body = located(record("alpha", PREFIX, definitions=("net", "dns")))
        assert ingest.apply(event(RUN_EVENT, body)) == APPLIED
        (run,) = rows(sessions, db.Run)
        assert (run.store, run.deployment, run.record_id) == ("prod", "alpha", "R1")
        assert {d.definition for d in rows(sessions, db.RunDefinition)} == {
            "net",
            "dns",
        }
        (deployment,) = rows(sessions, db.Deployment)
        assert deployment.prefix == PREFIX
        assert deployment.record_name is None
        (seen,) = rows(sessions, db.SeenObject)
        assert seen.key == f"{PREFIX}/_tfworker/runs/2026/11/27/R1.json"
        assert (seen.etag, seen.kind, seen.ref) == ("", "run", "R1")
        # a duplicate changes nothing
        assert ingest.apply(event(RUN_EVENT, body)) == UNCHANGED
        assert len(rows(sessions, db.Run)) == 1

    def test_record_name_kept_beside_the_segment(self, ingest, sessions):  # noqa: F811
        body = located(record("Alpha Prod", PREFIX))
        ingest.apply(event(RUN_EVENT, body, deployment="Alpha Prod"))
        (deployment,) = rows(sessions, db.Deployment)
        assert (deployment.name, deployment.record_name) == ("alpha", "Alpha Prod")
        assert deployment.discovered_from == "record"

    def test_status_out_of_order(self, ingest, sessions):  # noqa: F811
        older = located(status("alpha", PREFIX, run="run-R1"))
        newer = located(status("alpha", PREFIX, run="run-R2"))
        newer.updated_at = NOW + timedelta(minutes=10)
        assert ingest.apply(event(STATUS_EVENT, newer)) == APPLIED
        assert ingest.apply(event(STATUS_EVENT, older)) == UNCHANGED
        assert ingest.apply(event(STATUS_EVENT, newer)) == UNCHANGED
        (row,) = rows(sessions, db.Status)
        assert row.body["updated_by_run"] == "run-R2"
        assert {d.name for d in rows(sessions, db.Definition)} == {"net"}
        (seen,) = rows(sessions, db.SeenObject)
        assert seen.key == f"{PREFIX}/_tfworker/status/net.json"

    def test_plan_history_only_grows(self, ingest, sessions):  # noqa: F811
        pending = manifest("alpha", PREFIX)
        pending.state = BackendLocation(
            backend="s3",
            bucket="prod-state",
            state_key=f"{PREFIX}/net/terraform.tfstate",
        )
        applied = pending.moved("applying", at=NOW).moved("applied", at=NOW)
        assert ingest.apply(event(PLAN_EVENT, pending)) == APPLIED
        assert ingest.apply(event(PLAN_EVENT, applied)) == APPLIED
        assert ingest.apply(event(PLAN_EVENT, pending)) == UNCHANGED
        assert ingest.apply(event(PLAN_EVENT, applied)) == UNCHANGED
        (row,) = rows(sessions, db.Plan)
        assert row.status == "applied"
        (seen,) = rows(sessions, db.SeenObject)
        assert seen.key == f"{PREFIX}/_tfworker/plans/net/P1.json"

    def test_local_plans_are_ignored(self, ingest, sessions):  # noqa: F811
        local = located(manifest("alpha", PREFIX))
        local.storage = "local"
        assert ingest.apply(event(PLAN_EVENT, local)) == IGNORED
        assert rows(sessions, db.Plan) == []

    def test_lock_wait(self, ingest, sessions):  # noqa: F811
        at = NOW + timedelta(minutes=1)
        assert ingest.apply(event(LOCK_WAITING_EVENT, lock_wait(), at=at)) == APPLIED
        (lock,) = rows(sessions, db.Lock)
        assert (lock.deployment, lock.definition, lock.lock_id) == (
            "alpha",
            "net",
            "lock-1",
        )
        assert lock.observed_at == at and lock.lease is None
        # older, or the same again: nothing changes
        assert ingest.apply(event(LOCK_WAITING_EVENT, lock_wait(), at=NOW)) == UNCHANGED
        assert ingest.apply(event(LOCK_WAITING_EVENT, lock_wait(), at=at)) == UNCHANGED
        # later, same lock: only when it was seen moves
        # later, same lock: the lease and when it was read stay together
        later = at + timedelta(minutes=1)
        assert (
            ingest.apply(event(LOCK_WAITING_EVENT, lock_wait(), at=later)) == UNCHANGED
        )
        (lock,) = rows(sessions, db.Lock)
        assert lock.observed_at == at
        # a different lock replaces it
        newer = later + timedelta(minutes=1)
        wait = lock_wait(lock_id="lock-2")
        assert ingest.apply(event(LOCK_WAITING_EVENT, wait, at=newer)) == APPLIED
        (lock,) = rows(sessions, db.Lock)
        assert lock.lock_id == "lock-2"

    def test_lock_wait_keeps_a_known_lease(self, ingest, sessions):  # noqa: F811
        ingest.apply(event(LOCK_WAITING_EVENT, lock_wait()))
        with sessions() as session:
            session.get(db.Lock, ("prod", "alpha", "net")).lease = {"host": "pod-1"}
            session.commit()
        later = NOW + timedelta(minutes=1)
        ingest.apply(event(LOCK_WAITING_EVENT, lock_wait(), at=later))
        (lock,) = rows(sessions, db.Lock)
        assert lock.lease == {"host": "pod-1"}
        assert lock.observed_at == NOW

    def test_nothing_is_written_when_nothing_changes(
        self, ingest, sessions  # noqa: F811
    ):
        ingest.apply(event(RUN_EVENT, located(record("alpha", PREFIX))))
        # the same record id, placed in another deployment
        again = located(record("beta", "terraform/state/beta"))
        assert ingest.apply(event(RUN_EVENT, again)) == UNCHANGED
        assert {d.name for d in rows(sessions, db.Deployment)} == {"alpha"}

    def test_does_not_fit(self, ingest, sessions):  # noqa: F811
        error = DataError("INSERT", {}, Exception("value too long"))
        with mock.patch.object(ingest.reconciler, "_project", side_effect=error):
            with pytest.raises(InvalidEvent, match="does not fit"):
                ingest.apply(event(STATUS_EVENT, located(status("alpha", PREFIX))))
        assert rows(sessions, db.Status) == []


class TestTakingTurns:
    def test_events_older_than_the_last_reconcile_are_skipped(
        self, aws, ingest, reconciler, sessions  # noqa: F811
    ):
        bucket("prod-state")
        bucket("dev-state")
        reconciler.reconcile("prod")  # finds no lock, at NOW
        late = event(LOCK_WAITING_EVENT, lock_wait(), at=NOW - timedelta(minutes=10))
        assert ingest.apply(late) == UNCHANGED
        assert rows(sessions, db.Lock) == []
        old = located(status("alpha", PREFIX))
        old.updated_at = NOW - timedelta(seconds=1)
        assert ingest.apply(event(STATUS_EVENT, old)) == UNCHANGED
        newer = located(status("alpha", PREFIX))
        newer.updated_at = NOW + timedelta(seconds=1)
        assert ingest.apply(event(STATUS_EVENT, newer)) == APPLIED

    def test_a_failed_reconcile_does_not_move_the_mark(
        self, aws, ingest, reconciler, sessions  # noqa: F811
    ):
        # no buckets: the reconcile fails as a whole
        reconciler.reconcile("prod")
        (store,) = rows(sessions, db.Store, name="prod")
        assert store.last_error and store.reconcile_started_at is None

    def test_busy_while_this_process_reconciles(self, ingest, reconciler):
        body = event(STATUS_EVENT, located(status("alpha", PREFIX)))
        with reconciler.turn("prod") as had:
            assert had
            with pytest.raises(StoreBusy, match="prod"):
                ingest.apply(body)
        assert ingest.apply(body) == APPLIED

    def test_busy_while_another_replica_reconciles(self, ingest):
        body = event(STATUS_EVENT, located(status("alpha", PREFIX)))
        with mock.patch.object(ingest, "_lock_store", return_value=None):
            with pytest.raises(StoreBusy):
                ingest.apply(body)


class TestPlacing:
    def test_unknown_bucket(self, ingest, sessions):  # noqa: F811
        body = located(record("alpha", PREFIX), bucket_name="elsewhere")
        assert ingest.apply(event(RUN_EVENT, body)) == IGNORED
        assert rows(sessions, db.Deployment) == []

    def test_key_outside_every_template(self, ingest):
        body = located(record("alpha", "other/alpha"))
        assert ingest.apply(event(RUN_EVENT, body)) == IGNORED

    def test_by_name_without_a_location(self, ingest, sessions):  # noqa: F811
        # nothing is known about "alpha" yet
        body = record("alpha", PREFIX, definitions=())
        assert ingest.apply(event(RUN_EVENT, body)) == IGNORED
        ingest.apply(event(STATUS_EVENT, located(status("alpha", PREFIX))))
        assert ingest.apply(event(RUN_EVENT, body)) == APPLIED
        assert rows(sessions, db.Run)[0].deployment == "alpha"

    def test_by_name_is_refused_when_ambiguous(self, ingest):
        ingest.apply(event(STATUS_EVENT, located(status("alpha", PREFIX))))
        dev = located(status("alpha", "tf/alpha"), bucket_name="dev-state")
        ingest.apply(event(STATUS_EVENT, dev))
        body = record("alpha", PREFIX, record_id="R2", definitions=())
        assert ingest.apply(event(RUN_EVENT, body)) == IGNORED

    def test_lock_path_without_bucket_falls_back_to_name(
        self, ingest, sessions  # noqa: F811
    ):
        ingest.apply(event(STATUS_EVENT, located(status("alpha", PREFIX))))
        wait = lock_wait(path="no-slash")
        assert ingest.apply(event(LOCK_WAITING_EVENT, wait)) == APPLIED
        assert rows(sessions, db.Lock)[0].deployment == "alpha"

    def test_bucketless_location_matches_by_template(
        self, ingest, sessions  # noqa: F811
    ):
        body = record("alpha", PREFIX)  # the location names no bucket
        assert ingest.apply(event(RUN_EVENT, body)) == APPLIED
        assert rows(sessions, db.Run)[0].store == "prod"

    def test_segment_of(self):
        assert PROD.segment_of(f"{PREFIX}/net/terraform.tfstate") == "alpha"
        assert PROD.segment_of("terraform/state/") is None
        assert PROD.segment_of("terraform/other/x") is None
        middle = StoreConfig(
            name="m", region="r", bucket="b", prefix="a/{deployment}/state"
        )
        assert middle.segment_of("a/one/state/net/terraform.tfstate") == "one"
        assert middle.segment_of("a/one/other/net/terraform.tfstate") is None


class TestInvalid:
    def test_unknown_type_is_ignored(self, ingest):
        assert ingest.apply({"type": "com.example.other", "data": {}}) == IGNORED

    def test_no_data(self, ingest):
        with pytest.raises(InvalidEvent, match="no data"):
            ingest.apply({"type": RUN_EVENT})

    def test_unreadable_data(self, ingest):
        with pytest.raises(InvalidEvent, match="unreadable"):
            ingest.apply({"type": STATUS_EVENT, "data": {"deployment": "alpha"}})
        with pytest.raises(InvalidEvent, match="missing"):
            ingest.apply(
                {
                    "type": LOCK_WAITING_EVENT,
                    "time": NOW.isoformat(),
                    "data": {"deployment": "a"},
                }
            )

    def test_not_a_string_type(self, ingest):
        assert ingest.apply({"type": ["x"], "data": {}}) == IGNORED

    def test_lock_waits_need_a_time(self, ingest):
        body = event(LOCK_WAITING_EVENT, lock_wait())
        del body["time"]
        with pytest.raises(InvalidEvent, match="time"):
            ingest.apply(body)
        body["time"] = "9999-12-31T23:59:59-10:00"
        with pytest.raises(InvalidEvent):
            ingest.apply(body)

    def test_others_go_by_what_they_carry(self, ingest):
        body = event(STATUS_EVENT, located(status("alpha", PREFIX)))
        del body["time"]
        assert ingest.apply(body) == APPLIED

    def test_naive_time_is_utc(self, ingest, sessions):  # noqa: F811
        body = event(LOCK_WAITING_EVENT, lock_wait())
        body["time"] = "2026-11-27T12:00:00"
        ingest.apply(body)
        assert rows(sessions, db.Lock)[0].observed_at == NOW


class TestReconcilerStaysTheSource:
    def test_reconcile_reads_what_an_event_wrote(
        self, aws, ingest, reconciler, sessions  # noqa: F811
    ):
        s3 = bucket("prod-state")
        bucket("dev-state")
        body = located(record("alpha", PREFIX))
        put(s3, "prod-state", f"{PREFIX}/_tfworker/runs/2026/11/27/R1.json", body)
        ingest.apply(event(RUN_EVENT, body))
        summary = reconciler.reconcile("prod")
        assert (summary.fetched, summary.removed, summary.errors) == (1, 0, [])
        (seen,) = rows(sessions, db.SeenObject)
        assert seen.etag != ""
        assert len(rows(sessions, db.Run)) == 1

    def test_reconcile_drops_what_the_bucket_lacks(
        self, aws, ingest, reconciler, sessions  # noqa: F811
    ):
        s3 = bucket("prod-state")
        bucket("dev-state")
        put(s3, "prod-state", f"{PREFIX}/net/terraform.tfstate", "{}")
        ingest.apply(event(STATUS_EVENT, located(status("alpha", PREFIX))))
        ingest.apply(event(LOCK_WAITING_EVENT, lock_wait()))
        reconciler.reconcile("prod")
        assert rows(sessions, db.Status) == []
        assert rows(sessions, db.Lock) == []
        assert rows(sessions, db.SeenObject) == []


class TestEnvelope:
    def test_structured(self):
        body = event(STATUS_EVENT, located(status("alpha", PREFIX)))
        assert envelope(json.dumps(body)) == json.loads(json.dumps(body))

    def test_from_eventbridge(self):
        body = event(STATUS_EVENT, located(status("alpha", PREFIX)))
        wrapped = {"detail-type": STATUS_EVENT, "source": "tfworker", "detail": body}
        assert envelope(json.dumps(wrapped))["id"] == body["id"]

    def test_not_an_event(self):
        with pytest.raises(InvalidEvent, match="not JSON"):
            envelope("{")
        with pytest.raises(InvalidEvent, match="not a CloudEvent"):
            envelope(json.dumps({"detail": "x"}))
        with pytest.raises(InvalidEvent, match="not a CloudEvent"):
            envelope("[1]")

    def test_queue_region(self):
        url = "https://sqs.eu-west-2.amazonaws.com/123456789012/events"
        assert _queue_region(url) == "eu-west-2"
        assert _queue_region("http://localhost:4566/000000000000/events") is None


@pytest.fixture
def queue(aws):  # noqa: F811
    sqs = boto3.client("sqs", region_name="us-east-1")
    url = sqs.create_queue(QueueName="events", Attributes={"VisibilityTimeout": "0"})[
        "QueueUrl"
    ]
    return sqs, url


class TestConsumer:
    def test_poll(self, queue, ingest, sessions):  # noqa: F811
        sqs, url = queue
        good = event(STATUS_EVENT, located(status("alpha", PREFIX)))
        sqs.send_message(QueueUrl=url, MessageBody=json.dumps(good))
        sqs.send_message(QueueUrl=url, MessageBody="not json")
        consumer = EventConsumer(ingest, url, client=sqs)
        consumer.WAIT_SECONDS = 0
        assert consumer.poll() == 2
        assert len(rows(sessions, db.Status)) == 1
        assert consumer.poll() == 0

    def test_a_failure_is_left_for_redelivery(self, queue, ingest):
        sqs, url = queue
        good = event(STATUS_EVENT, located(status("alpha", PREFIX)))
        sqs.send_message(QueueUrl=url, MessageBody=json.dumps(good))
        consumer = EventConsumer(ingest, url, client=sqs)
        consumer.WAIT_SECONDS = 0
        with mock.patch.object(ingest, "apply", side_effect=RuntimeError("db down")):
            assert consumer.poll() == 0
        assert consumer.poll() == 1

    def test_busy_comes_back_sooner(self, queue, ingest):
        sqs, url = queue
        good = event(STATUS_EVENT, located(status("alpha", PREFIX)))
        sqs.send_message(QueueUrl=url, MessageBody=json.dumps(good))
        consumer = EventConsumer(ingest, url, client=sqs)
        consumer.WAIT_SECONDS = 0
        consumer.BUSY_SECONDS = 0
        with mock.patch.object(ingest, "apply", side_effect=StoreBusy("prod")):
            with mock.patch.object(
                sqs, "change_message_visibility", wraps=sqs.change_message_visibility
            ) as later:
                assert consumer.poll() == 0
                assert later.call_args.kwargs["VisibilityTimeout"] == 0
        assert consumer.poll() == 1

    def test_busy_message_that_cannot_be_moved(self, ingest):
        client = mock.Mock()
        client.receive_message.return_value = {
            "Messages": [{"Body": "{}", "ReceiptHandle": "h"}]
        }
        client.change_message_visibility.side_effect = RuntimeError("gone")
        consumer = EventConsumer(
            ingest, "https://sqs.us-east-1.amazonaws.com/1/q", client=client
        )
        with mock.patch("tfworker_console.ingest.envelope", return_value={}):
            with mock.patch.object(ingest, "apply", side_effect=StoreBusy("prod")):
                assert consumer.poll() == 0

    def test_failed_deletes_are_not_counted(self, ingest):
        client = mock.Mock()
        client.receive_message.return_value = {
            "Messages": [{"Body": "nope", "ReceiptHandle": "h", "MessageId": "m"}]
        }
        client.delete_message_batch.return_value = {"Failed": [{"Id": "0"}]}
        consumer = EventConsumer(
            ingest, "https://sqs.us-east-1.amazonaws.com/1/q", client=client
        )
        assert consumer.poll() == 0

    def test_client_is_rebuilt(self, aws):  # noqa: F811
        clock = mock.Mock(return_value=0.0)
        consumer = EventConsumer(
            mock.Mock(), "https://sqs.us-east-1.amazonaws.com/1/q", clock=clock
        )
        first = consumer.client()
        assert consumer.client() is first
        clock.return_value = consumer.CLIENT_SECONDS
        second = consumer.client()
        assert second is not first
        # an error drops it, so the next poll starts from fresh credentials
        consumer.WAIT_SECONDS = 0.01

        def poll():
            consumer._stop.set()
            raise RuntimeError("ExpiredToken")

        with mock.patch.object(consumer, "poll", side_effect=poll):
            consumer._loop()
        assert consumer._client is None

    def test_observers(self, ingest):
        consumer = EventConsumer(ingest, "https://sqs.us-east-1.amazonaws.com/1/q")
        seen = []
        consumer.observers.append(lambda kind, outcome: seen.append((kind, outcome)))
        consumer.observers.append(mock.Mock(side_effect=RuntimeError("broken")))
        good = event(STATUS_EVENT, located(status("alpha", PREFIX)))
        assert consumer._handle({"Body": json.dumps(good)}) is True
        assert consumer._handle({"Body": "nope"}) is True
        assert seen == [(STATUS_EVENT, APPLIED), ("unknown", DROPPED)]

    def test_client_from_credentials(self, aws):  # noqa: F811
        consumer = EventConsumer(
            mock.Mock(), "https://sqs.us-west-2.amazonaws.com/1/q", Credentials()
        )
        assert consumer.region == "us-west-2"
        assert consumer.client().meta.region_name == "us-west-2"

    def test_loop_runs_until_stopped(self, ingest):
        consumer = EventConsumer(ingest, "https://sqs.us-east-1.amazonaws.com/1/q")
        consumer.WAIT_SECONDS = 0.01
        calls = []

        def poll():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("throttled")
            if len(calls) == 3:
                consumer._stop.set()
            return 0

        with mock.patch.object(consumer, "poll", side_effect=poll):
            consumer.start()
            consumer._thread.join(5)
            consumer.stop()
        assert len(calls) == 3

    def test_stop_while_backing_off(self, ingest):
        consumer = EventConsumer(ingest, "https://sqs.us-east-1.amazonaws.com/1/q")

        def fail():
            consumer._stop.set()
            raise RuntimeError("throttled")

        with mock.patch.object(consumer, "poll", side_effect=fail):
            consumer._loop()  # returns rather than waiting out the back-off


class TestApp:
    def test_needs_a_database(self):
        settings = ConsoleSettings(
            events_queue_url="https://sqs.us-east-1.amazonaws.com/1/q"
        )
        with pytest.raises(ValueError, match="DATABASE_URL"):
            create_app(settings)

    def test_needs_stores(self, tmp_path):
        settings = ConsoleSettings(
            database_url=f"sqlite:///{tmp_path}/c.db",
            auto_migrate=True,
            events_queue_url="https://sqs.us-east-1.amazonaws.com/1/q",
        )
        with pytest.raises(ValueError, match="STORES_FILE"):
            create_app(settings)

    def test_consumer_runs_while_serving(self, tmp_path):
        stores = tmp_path / "stores.yaml"
        stores.write_text(
            "stores:\n  - name: prod\n    region: us-east-1\n"
            "    bucket: prod-state\n    prefix: terraform/state/{deployment}\n"
        )
        settings = ConsoleSettings(
            database_url=f"sqlite:///{tmp_path}/c.db",
            auto_migrate=True,
            stores_file=str(stores),
            reconcile_interval=0,
            events_queue_url="https://sqs.us-east-1.amazonaws.com/1/q",
            events_role_arn="arn:aws:iam::123456789012:role/events",
        )
        with (
            mock.patch.object(EventConsumer, "start") as start,
            mock.patch.object(EventConsumer, "stop") as stop,
        ):
            app = create_app(settings)
            with TestClient(app):
                start.assert_called_once()
            stop.assert_called_once()
