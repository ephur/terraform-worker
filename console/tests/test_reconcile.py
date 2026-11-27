import json
import os
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from click.testing import CliRunner
from moto import mock_aws
from sqlalchemy import func, select, text
from tfworker.report import (
    BackendLocation,
    DefinitionResult,
    DefinitionStatus,
    PlanManifest,
    RunRecord,
)

from tfworker_console import db
from tfworker_console.cli import main
from tfworker_console.migrate import upgrade
from tfworker_console.reconcile import Reconciler, classify
from tfworker_console.scheduler import ReconcileScheduler
from tfworker_console.stores import Credentials, StoreConfig, load_stores

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
ROLE = "arn:aws:iam::123456789012:role/console-read"


@pytest.fixture
def aws(monkeypatch):
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        yield


@pytest.fixture
def engine(tmp_path):
    # CI also runs these against Postgres
    url = os.environ.get("TFWORKER_CONSOLE_TEST_DATABASE_URL")
    engine = db.engine_for(url or f"sqlite:///{tmp_path}/console.db")
    if url:  # pragma: no cover
        with engine.begin() as connection:
            db.Base.metadata.drop_all(connection)
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    upgrade(engine)
    return engine


@pytest.fixture
def sessions(engine):
    return db.session_factory(engine)


def bucket(name, region="us-east-1"):
    s3 = boto3.client("s3", region_name=region)
    if region == "us-east-1":
        s3.create_bucket(Bucket=name)
    else:
        s3.create_bucket(
            Bucket=name, CreateBucketConfiguration={"LocationConstraint": region}
        )
    return s3


def put(s3, bucket_name, key, body):
    if not isinstance(body, str):
        body = body.model_dump_json(by_alias=True)
    s3.put_object(Bucket=bucket_name, Key=key, Body=body.encode())


def location(prefix, definition):
    return BackendLocation(
        backend="s3", state_key=f"{prefix}/{definition}/terraform.tfstate"
    )


def record(deployment, prefix, record_id="R1", definitions=("net",)):
    return RunRecord(
        record_id=record_id,
        run_id=f"run-{record_id}",
        deployment=deployment,
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=3),
        status="succeeded",
        actions=["plan"],
        definitions=[
            DefinitionResult(definition=name, state=location(prefix, name))
            for name in definitions
        ],
    )


def status(deployment, prefix, definition="net", run="run-R1"):
    return DefinitionStatus(
        deployment=deployment,
        definition=definition,
        state=location(prefix, definition),
        updated_at=NOW,
        updated_by_run=run,
    )


def manifest(deployment, prefix, plan_id="P1", definition="net"):
    return PlanManifest(
        plan_id=plan_id,
        run_id="run-R1",
        deployment=deployment,
        definition=definition,
        plan_key=f"{prefix}/{definition}/run-R1.tfplan",
        sha256="0" * 64,
        size=10,
        created_at=NOW,
    )


def seed(s3, bucket_name, prefix, deployment):
    put(s3, bucket_name, f"{prefix}/net/terraform.tfstate", '{"secret": "never read"}')
    put(s3, bucket_name, f"{prefix}/dns/terraform.tfstate", "{}")
    put(
        s3,
        bucket_name,
        f"{prefix}/_tfworker/runs/2026/11/27/R1.json",
        record(deployment, prefix),
    )
    put(
        s3,
        bucket_name,
        f"{prefix}/_tfworker/status/net.json",
        status(deployment, prefix),
    )
    put(
        s3,
        bucket_name,
        f"{prefix}/_tfworker/plans/net/P1.json",
        manifest(deployment, prefix),
    )
    put(s3, bucket_name, f"{prefix}/_tfworker/pending/net/P1", "")


def count(sessions, model):
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(model))


def lock_body(who="runner@pod-1", lock_id="lock-1"):
    return json.dumps(
        {
            "ID": lock_id,
            "Operation": "OperationTypeApply",
            "Who": who,
            "Created": (NOW - timedelta(minutes=5)).isoformat(),
        }
    )


def lease_body():
    return json.dumps(
        {
            "definition": "net",
            "action": "apply",
            "who": "runner@pod-1",
            "host": "pod-1",
            "pid": 7,
            "started_at": NOW.isoformat(),
            "heartbeat_at": NOW.isoformat(),
            "heartbeat_seconds": 30,
        }
    )


@pytest.fixture
def two_stores(aws):
    prod = bucket("prod-state")
    dev = bucket("dev-state", region="us-west-2")
    seed(prod, "prod-state", "terraform/state/alpha", "alpha")
    seed(prod, "prod-state", "terraform/state/beta", "beta")
    seed(dev, "dev-state", "tf/sandbox", "sandbox")
    return [
        StoreConfig(
            name="prod",
            region="us-east-1",
            bucket="prod-state",
            prefix="terraform/state/{deployment}",
            credentials=Credentials(role_arn=ROLE, external_id="console"),
        ),
        StoreConfig(
            name="dev",
            region="us-west-2",
            bucket="dev-state",
            prefix="tf/{deployment}",
        ),
    ]


class TestReconcile:
    def test_two_stores_with_different_credentials(self, two_stores, sessions, mocker):
        calls = []
        real_client = boto3.client

        def client(service, **kwargs):
            made = real_client(service, **kwargs)
            if service == "sts":
                original = made.assume_role

                def assume_role(**params):
                    calls.append(params)
                    return original(**params)

                made.assume_role = assume_role
            return made

        mocker.patch("tfworker_console.stores.boto3.client", side_effect=client)
        summaries = Reconciler(two_stores, sessions, now=lambda: NOW).reconcile_all()
        assert [(s.store, s.deployments, s.fetched, s.errors) for s in summaries] == [
            ("prod", 2, 6, []),
            ("dev", 1, 3, []),
        ]
        assert calls == [
            {
                "RoleArn": ROLE,
                "RoleSessionName": "tfworker-console",
                "ExternalId": "console",
            }
        ]
        with sessions() as session:
            stores = {row.name: row for row in session.scalars(select(db.Store))}
            assert stores["prod"].credentials == "role"
            assert stores["dev"].credentials == "ambient"
            assert stores["dev"].reconciled_at is not None
            deployments = session.execute(
                select(db.Deployment.store, db.Deployment.name, db.Deployment.prefix)
            ).all()
            assert sorted(deployments) == [
                ("dev", "sandbox", "tf/sandbox"),
                ("prod", "alpha", "terraform/state/alpha"),
                ("prod", "beta", "terraform/state/beta"),
            ]
            definitions = session.execute(
                select(
                    db.Definition.store,
                    db.Definition.deployment,
                    db.Definition.name,
                    db.Definition.state_key,
                )
            ).all()
            assert (
                "prod",
                "alpha",
                "dns",
                "terraform/state/alpha/dns/terraform.tfstate",
            ) in definitions
            assert len(definitions) == 6
            run = session.get(db.Run, ("prod", "R1"))
            assert run is not None and run.status == "succeeded"
            assert session.get(db.Plan, ("dev", "P1")).status == "pending"
            assert (
                session.get(db.Status, ("prod", "beta", "net")).body["updated_by_run"]
                == "run-R1"
            )

    def test_state_bodies_are_never_read(self, two_stores, sessions, mocker):
        gets = []
        from botocore.client import BaseClient

        original = BaseClient._make_api_call

        def spy(self, operation, params):
            if operation == "GetObject":
                gets.append(params["Key"])
            return original(self, operation, params)

        mocker.patch.object(BaseClient, "_make_api_call", spy)
        Reconciler(two_stores, sessions).reconcile_all()
        assert gets
        assert not [key for key in gets if key.endswith("terraform.tfstate")]

    def test_rereconcile_is_idempotent_and_skips_unchanged(self, two_stores, sessions):
        reconciler = Reconciler(two_stores, sessions)
        reconciler.reconcile_all()
        before = {
            model: count(sessions, model)
            for model in (db.Run, db.Status, db.Plan, db.Definition, db.Deployment)
        }
        again = reconciler.reconcile_all()
        assert [(s.fetched, s.skipped) for s in again] == [(0, 6), (0, 3)]
        assert {model: count(sessions, model) for model in before} == before

    def test_changed_object_is_refetched(self, two_stores, sessions):
        reconciler = Reconciler(two_stores, sessions)
        reconciler.reconcile("dev")
        updated = manifest("sandbox", "tf/sandbox").model_copy(
            update={"status": "discarded"}
        )
        put(
            boto3.client("s3", region_name="us-west-2"),
            "dev-state",
            "tf/sandbox/_tfworker/plans/net/P1.json",
            updated,
        )
        summary = reconciler.reconcile("dev")
        assert (summary.fetched, summary.skipped) == (1, 2)
        with sessions() as session:
            assert session.get(db.Plan, ("dev", "P1")).status == "discarded"

    def test_removed_objects_leave_the_projection(self, two_stores, sessions):
        reconciler = Reconciler(two_stores, sessions)
        reconciler.reconcile("dev")
        s3 = boto3.client("s3", region_name="us-west-2")
        s3.delete_object(
            Bucket="dev-state", Key="tf/sandbox/_tfworker/plans/net/P1.json"
        )
        s3.delete_object(Bucket="dev-state", Key="tf/sandbox/_tfworker/status/net.json")
        summary = reconciler.reconcile("dev")
        assert summary.removed == 2
        with sessions() as session:
            assert session.get(db.Plan, ("dev", "P1")) is None
            assert session.get(db.Status, ("dev", "sandbox", "net")) is None
            assert session.get(db.Run, ("dev", "R1")) is not None

    def test_rebuild_from_empty(self, two_stores, sessions):
        reconciler = Reconciler(two_stores, sessions)
        reconciler.reconcile_all()
        with sessions() as session:
            session.get(db.Plan, ("prod", "P1")).status = "tampered"
            session.commit()
        summaries = reconciler.rebuild()
        assert [s.fetched for s in summaries] == [6, 3]
        with sessions() as session:
            assert session.get(db.Plan, ("prod", "P1")).status == "pending"
        assert count(sessions, db.Run) == 2


class TestDiscovery:
    def test_from_template_without_records(self, aws, sessions):
        s3 = bucket("state")
        put(s3, "state", "env/gamma/vpc/terraform.tfstate", "{}")
        put(s3, "state", "env/empty/readme.txt", "x")
        store = StoreConfig(
            name="s", region="us-east-1", bucket="state", prefix="env/{deployment}"
        )
        Reconciler([store], sessions).reconcile("s")
        with sessions() as session:
            (row,) = session.scalars(select(db.Deployment))
            assert (row.name, row.discovered_from) == ("gamma", "template")

    def test_name_from_run_records(self, aws, sessions):
        s3 = bucket("state")
        seed(s3, "state", "env/prod-us", "production")
        store = StoreConfig(
            name="s", region="us-east-1", bucket="state", prefix="env/{deployment}"
        )
        reconciler = Reconciler([store], sessions)
        reconciler.reconcile("s")
        reconciler.reconcile("s")
        with sessions() as session:
            (row,) = session.scalars(select(db.Deployment))
            assert (row.name, row.prefix, row.discovered_from) == (
                "production",
                "env/prod-us",
                "record",
            )
            assert session.get(db.Status, ("s", "production", "net")) is not None

    def test_unreadable_record_falls_back_to_segment(self, aws, sessions):
        s3 = bucket("state")
        put(s3, "state", "env/delta/_tfworker/runs/2026/11/27/R9.json", "not json")
        store = StoreConfig(
            name="s", region="us-east-1", bucket="state", prefix="env/{deployment}"
        )
        summary = Reconciler([store], sessions).reconcile("s")
        assert summary.errors == [
            "env/delta/_tfworker/runs/2026/11/27/R9.json: ValidationError"
        ]
        with sessions() as session:
            assert session.get(db.Deployment, ("s", "delta")) is not None
            assert session.get(db.Store, "s").last_error.startswith("env/delta")

    def test_template_with_a_tail(self, aws, sessions):
        s3 = bucket("state")
        seed(s3, "state", "org/omega/terraform", "omega")
        store = StoreConfig(
            name="s",
            region="us-east-1",
            bucket="state",
            prefix="org/{deployment}/terraform",
        )
        assert Reconciler([store], sessions).reconcile("s").fetched == 3


class TestLocks:
    def test_held_lock_with_lease_then_released(self, aws, sessions):
        s3 = bucket("state")
        seed(s3, "state", "env/alpha", "alpha")
        put(s3, "state", "env/alpha/net/terraform.tfstate.tflock", lock_body())
        put(
            s3,
            "state",
            "env/alpha/net/terraform.tfstate.tfworker-lease.pod-1",
            lease_body(),
        )
        put(
            s3,
            "state",
            "env/alpha/dns/terraform.tfstate.tflock",
            lock_body(who="person@laptop", lock_id="lock-2"),
        )
        store = StoreConfig(
            name="s", region="us-east-1", bucket="state", prefix="env/{deployment}"
        )
        reconciler = Reconciler([store], sessions, now=lambda: NOW)
        assert reconciler.reconcile("s").locks == 2
        with sessions() as session:
            net = session.get(db.Lock, ("s", "alpha", "net"))
            assert (net.lock_id, net.who, net.operation) == (
                "lock-1",
                "runner@pod-1",
                "OperationTypeApply",
            )
            assert net.lease["pid"] == 7
            assert session.get(db.Lock, ("s", "alpha", "dns")).lease is None
        s3.delete_object(Bucket="state", Key="env/alpha/net/terraform.tfstate.tflock")
        reconciler.reconcile("s")
        with sessions() as session:
            assert session.get(db.Lock, ("s", "alpha", "net")) is None
            assert session.get(db.Lock, ("s", "alpha", "dns")) is not None

    def test_unreadable_lock_and_lease(self, aws, sessions):
        s3 = bucket("state")
        put(s3, "state", "env/alpha/net/terraform.tfstate.tflock", "garbage")
        put(s3, "state", "env/alpha/vpc/terraform.tfstate.tflock", lock_body(who=""))
        put(s3, "state", "env/alpha/dns/terraform.tfstate.tflock", lock_body())
        put(
            s3,
            "state",
            "env/alpha/dns/terraform.tfstate.tfworker-lease.pod-1",
            "garbage",
        )
        store = StoreConfig(
            name="s", region="us-east-1", bucket="state", prefix="env/{deployment}"
        )
        summary = Reconciler([store], sessions).reconcile("s")
        assert summary.locks == 2
        assert summary.errors == [
            "env/alpha/net/terraform.tfstate.tflock: ValidationError"
        ]
        with sessions() as session:
            assert session.get(db.Lock, ("s", "alpha", "dns")).lease is None


class TestFailures:
    def test_a_failing_store_does_not_stop_the_others(self, two_stores, sessions):
        broken = StoreConfig(
            name="broken", region="us-east-1", bucket="missing", prefix="x/{deployment}"
        )
        summaries = Reconciler([broken] + two_stores, sessions).reconcile_all()
        assert summaries[0].errors and "NoSuchBucket" in summaries[0].errors[0]
        assert summaries[1].fetched == 6
        with sessions() as session:
            assert "NoSuchBucket" in session.get(db.Store, "broken").last_error


def test_classify_ignores_unknown_keys():
    listing = classify(
        "p",
        [
            ("p/_tfworker/pending/net/P1", "e"),
            ("p/_tfworker/previews/R/net.tfplan", "e"),
            ("p/_tfworker/runs/2026/R.json", "e"),
            ("p/net/sub/terraform.tfstate", "e"),
            ("p/net/other.txt", "e"),
        ],
    )
    assert list(listing.reports) == ["p/_tfworker/runs/2026/R.json"]
    assert listing.states == {} and listing.locks == {} and listing.leases == {}


class TestStoresConfig:
    def test_load(self, tmp_path):
        path = tmp_path / "stores.yaml"
        path.write_text(
            "stores:\n"
            "  - {name: a, region: us-east-1, bucket: b, prefix: '/t/{deployment}/'}\n"
            "  - {name: c, region: us-east-1, bucket: d, prefix: '{deployment}', credentials: {profile: dev}}\n"
        )
        a, c = load_stores(str(path))
        assert a.prefix == "t/{deployment}" and a.listing_prefix == "t/"
        assert c.listing_prefix == "" and c.credentials.kind == "profile"
        assert load_stores(None) == []
        empty = tmp_path / "empty.yaml"
        empty.write_text("")
        assert load_stores(str(empty)) == []

    @pytest.mark.parametrize(
        "prefix",
        ["state", "{deployment}/{deployment}", "pre{deployment}", "{deployment}x/y"],
    )
    def test_bad_template(self, prefix):
        with pytest.raises(ValueError):
            StoreConfig(name="a", region="r", bucket="b", prefix=prefix)

    def test_bad_credentials_and_duplicates(self, tmp_path):
        with pytest.raises(ValueError):
            Credentials(role_arn=ROLE, profile="dev")
        with pytest.raises(ValueError):
            Credentials(external_id="x")
        path = tmp_path / "stores.yaml"
        path.write_text(
            "stores:\n"
            "  - {name: a, region: r, bucket: b, prefix: '{deployment}'}\n"
            "  - {name: a, region: r, bucket: c, prefix: '{deployment}'}\n"
        )
        with pytest.raises(ValueError, match="duplicate store names: a"):
            load_stores(str(path))

    def test_profile_session(self, tmp_path, monkeypatch):
        config = tmp_path / "config"
        config.write_text(
            "[profile dev]\naws_access_key_id = a\naws_secret_access_key = b\n"
        )
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        store = StoreConfig(
            name="a",
            region="eu-west-1",
            bucket="b",
            prefix="{deployment}",
            credentials=Credentials(profile="dev"),
        )
        session = store.session()
        assert session.profile_name == "dev"
        assert session.region_name == "eu-west-1"


class TestCli:
    @pytest.fixture
    def env(self, two_stores, tmp_path, monkeypatch, engine):
        path = tmp_path / "stores.yaml"
        path.write_text(json.dumps({"stores": [s.model_dump() for s in two_stores]}))
        monkeypatch.setenv("TFWORKER_CONSOLE_STORES_FILE", str(path))
        monkeypatch.setenv("TFWORKER_CONSOLE_DATABASE_URL", str(engine.url))

    def test_upgrade(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "TFWORKER_CONSOLE_DATABASE_URL", f"sqlite:///{tmp_path}/new.db"
        )
        result = CliRunner().invoke(main, ["db", "upgrade"])
        assert result.exit_code == 0, result.output
        assert "up to date" in result.output

    def test_needs_a_database(self, monkeypatch):
        monkeypatch.delenv("TFWORKER_CONSOLE_DATABASE_URL", raising=False)
        result = CliRunner().invoke(main, ["db", "upgrade"])
        assert result.exit_code == 2
        assert "TFWORKER_CONSOLE_DATABASE_URL" in result.output

    def test_reconcile_one_store(self, env):
        result = CliRunner().invoke(main, ["reconcile", "--store", "dev"])
        assert result.exit_code == 0, result.output
        (summary,) = json.loads(result.output)
        assert (summary["store"], summary["fetched"]) == ("dev", 3)

    def test_rebuild(self, env):
        result = CliRunner().invoke(main, ["reconcile", "--rebuild"])
        assert result.exit_code == 0, result.output
        assert [s["store"] for s in json.loads(result.output)] == ["prod", "dev"]

    def test_bad_arguments(self, env):
        assert CliRunner().invoke(main, ["reconcile", "--store", "nope"]).exit_code == 2
        assert (
            CliRunner()
            .invoke(main, ["reconcile", "--rebuild", "--store", "dev"])
            .exit_code
            == 2
        )

    def test_errors_exit_nonzero(self, env, two_stores):
        boto3.client("s3", region_name="us-west-2").put_object(
            Bucket="dev-state", Key="tf/sandbox/_tfworker/status/bad.json", Body=b"{}"
        )
        result = CliRunner().invoke(main, ["reconcile", "--store", "dev"])
        assert result.exit_code == 1


class TestScheduler:
    def test_runs_until_stopped(self, two_stores, sessions):
        scheduler = ReconcileScheduler(Reconciler(two_stores, sessions), interval=3600)
        scheduler.start()
        scheduler.stop()
        assert count(sessions, db.Run) == 2

    def test_a_failing_run_is_logged(self, mocker):
        reconciler = mocker.Mock()
        reconciler.reconcile_all.side_effect = RuntimeError("down")
        error = mocker.patch("tfworker_console.scheduler.log.error")
        ReconcileScheduler(reconciler, 1).run_once()
        assert "down" in error.call_args.args[0]

    def test_app_schedules_when_configured(self, two_stores, tmp_path, engine):
        from fastapi.testclient import TestClient

        from tfworker_console.app import create_app
        from tfworker_console.settings import ConsoleSettings

        path = tmp_path / "stores.yaml"
        path.write_text(json.dumps({"stores": [s.model_dump() for s in two_stores]}))
        settings = ConsoleSettings(
            database_url=str(engine.url), stores_file=str(path), reconcile_interval=3600
        )
        with TestClient(create_app(settings)) as client:
            assert client.get("/healthz").status_code == 200
        assert count(db.session_factory(engine), db.Run) == 2
