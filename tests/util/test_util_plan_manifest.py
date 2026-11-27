import json
from datetime import datetime, timedelta, timezone
from zipfile import ZipFile

import boto3
import botocore.exceptions
import pytest
from moto import mock_aws

from tfworker.exceptions import BackendError, ReportConflictError
from tfworker.report.plans import InvalidTransition, PlanManifest
from tfworker.util.plan_manifest import (
    LocalManifestStore,
    ManifestStore,
    S3ManifestStore,
    manifest_key,
    mark_pending_stale,
    move,
    new_manifest,
    plan_base,
    plan_changes,
    refusal,
    sha256_file,
)

T0 = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
BUCKET = "plans"


def make_plan(path, lineage="abc", serial=3, content="x"):
    with ZipFile(path, "w") as archive:
        archive.writestr("tfstate", json.dumps({"lineage": lineage, "serial": serial}))
        archive.writestr("content", content)
    return path


def manifest(plan_id="p1", definition="net", status="pending", **fields):
    values = dict(
        plan_id=plan_id,
        run_id="r1",
        deployment="dep",
        definition=definition,
        plan_key=f"prefix/{definition}/terraform.tfplan",
        sha256="0" * 64,
        size=1,
        created_at=T0,
        status=status,
    )
    values.update(fields)
    return PlanManifest(**values)


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


class TestHelpers:
    def test_manifest_key(self):
        assert manifest_key("a/b.tfplan") == "a/b.tfplan.manifest.json"

    def test_plan_base(self, tmp_path):
        plan = make_plan(tmp_path / "p.tfplan")
        assert plan_base(plan) == ("abc", 3)

    def test_plan_base_empty_lineage(self, tmp_path):
        plan = make_plan(tmp_path / "p.tfplan", lineage="", serial=0)
        assert plan_base(plan) == (None, 0)

    def test_plan_base_not_a_zip(self, tmp_path):
        plan = tmp_path / "p.tfplan"
        plan.write_text("nope")
        assert plan_base(plan) == (None, None)

    def test_plan_changes(self, tmp_path):
        plan = tmp_path / "p.tfplan"
        plan.with_suffix(".tfplan.json").write_text(
            json.dumps(
                {
                    "resource_changes": [
                        {"address": "a.b", "change": {"actions": ["create"]}}
                    ]
                }
            )
        )
        changes = plan_changes(plan)
        assert changes.add == 1
        assert changes.resources[0].address == "a.b"

    def test_plan_changes_missing(self, tmp_path):
        assert plan_changes(tmp_path / "p.tfplan") is None

    def test_new_manifest(self, tmp_path):
        plan = make_plan(tmp_path / "p.tfplan")
        made = new_manifest(
            run_id="r1",
            deployment="dep",
            definition="net",
            planfile=plan,
            plan_key="k",
            storage="local",
            now=T0,
            ttl_seconds=3600,
            destroy=True,
            base_state_version_id="v1",
        )
        assert made.status == "pending"
        assert made.destroy is True
        assert (made.base_lineage, made.base_serial) == ("abc", 3)
        assert made.sha256 == sha256_file(plan)
        assert made.size == plan.stat().st_size
        assert made.expires_at == T0 + timedelta(hours=1)
        assert made.base_state_version_id == "v1"
        assert len(made.plan_id) == 26

    def test_new_manifest_without_ttl_never_expires(self, tmp_path):
        plan = make_plan(tmp_path / "p.tfplan")
        made = new_manifest(
            run_id="r1",
            deployment="dep",
            definition="net",
            planfile=plan,
            plan_key="k",
            storage="local",
            now=T0,
            ttl_seconds=0,
        )
        assert made.expires_at is None


class TestRefusal:
    def test_accepts_matching_plan(self, tmp_path):
        plan = make_plan(tmp_path / "p.tfplan")
        m = manifest(sha256=sha256_file(plan), expires_at=T0 + timedelta(days=1))
        assert refusal(m, plan, T0) is None

    def test_not_pending(self, tmp_path):
        plan = make_plan(tmp_path / "p.tfplan")
        m = manifest(sha256=sha256_file(plan), status="applied")
        assert "applied, not pending" in refusal(m, plan, T0)

    def test_expired(self, tmp_path):
        plan = make_plan(tmp_path / "p.tfplan")
        m = manifest(sha256=sha256_file(plan), expires_at=T0)
        assert "expired" in refusal(m, plan, T0)

    def test_altered(self, tmp_path):
        plan = make_plan(tmp_path / "p.tfplan")
        assert "sha256" in refusal(manifest(), plan, T0)


class TestPlanManifestModel:
    def test_moved_records_history(self):
        m = manifest().moved("applying", at=T0, run_id="r2")
        assert m.status == "applying"
        assert m.history[-1].run_id == "r2"
        assert m.history[-1].at == T0

    def test_invalid_transition(self):
        with pytest.raises(InvalidTransition):
            manifest(status="applied").moved("applying")

    def test_naive_times_are_utc(self):
        m = manifest(created_at=datetime(2026, 1, 1))
        assert m.created_at.tzinfo is timezone.utc

    def test_round_trip_keeps_schema_alias(self):
        text = manifest().model_dump_json(by_alias=True)
        assert json.loads(text)["schema"] == "tfworker.plan/v1"
        assert PlanManifest.model_validate_json(text) == manifest()


class TestLocalManifestStore:
    def test_read_missing(self, tmp_path):
        assert LocalManifestStore().read(str(tmp_path / "m.json")) is None

    def test_write_and_read(self, tmp_path):
        store = LocalManifestStore()
        key = str(tmp_path / "m.json")
        store.write(key, manifest())
        found, tag = store.read(key)
        assert found == manifest()
        assert len(tag) == 64
        assert sorted(p.name for p in tmp_path.iterdir()) == [".m.json.lock", "m.json"]

    def test_create_only_conflict(self, tmp_path):
        store = LocalManifestStore()
        key = str(tmp_path / "m.json")
        store.write(key, manifest(), create_only=True)
        with pytest.raises(ReportConflictError):
            store.write(key, manifest(), create_only=True)

    def test_if_match_conflict(self, tmp_path):
        store = LocalManifestStore()
        key = str(tmp_path / "m.json")
        with pytest.raises(ReportConflictError):
            store.write(key, manifest(), if_match="nope")
        store.write(key, manifest())
        with pytest.raises(ReportConflictError):
            store.write(key, manifest(), if_match="nope")
        tag = store.read(key)[1]
        store.write(key, manifest(status="stale"), if_match=tag)
        assert store.read(key)[0].status == "stale"

    def test_unreadable_manifest(self, tmp_path):
        key = tmp_path / "m.json"
        key.write_text("{not json")
        found, tag = LocalManifestStore().read(str(key))
        assert found is None
        assert tag

    def test_read_error(self, tmp_path):
        with pytest.raises(BackendError):
            LocalManifestStore().read(str(tmp_path))

    def test_write_error(self, tmp_path):
        with pytest.raises(BackendError):
            LocalManifestStore().write(str(tmp_path / "missing" / "m.json"), manifest())

    def test_replace_error(self, tmp_path, monkeypatch):
        def fail(*args):
            raise OSError("read-only")

        monkeypatch.setattr("tfworker.util.plan_manifest.os.replace", fail)
        with pytest.raises(BackendError, match="read-only"):
            LocalManifestStore().write(str(tmp_path / "m.json"), manifest())

    def test_writers_take_turns(self, tmp_path):
        """A writer that read before another wrote loses its conditional write."""
        import threading

        store = LocalManifestStore()
        key = str(tmp_path / "m.json")
        store.write(key, manifest())
        tag = store.read(key)[1]
        results = []

        def take(run_id):
            try:
                store.write(
                    key, manifest().moved("applying", run_id=run_id), if_match=tag
                )
                results.append(run_id)
            except ReportConflictError:
                results.append("lost")

        threads = [threading.Thread(target=take, args=(f"r{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count("lost") == 7

    def test_keys_for_is_empty(self):
        assert list(LocalManifestStore().keys_for("net")) == []


class TestS3ManifestStore:
    def test_read_missing(self, s3):
        assert S3ManifestStore(s3, BUCKET, "prefix").read("prefix/x") is None

    def test_pending_backend_plan_is_indexed(self, s3):
        store = S3ManifestStore(s3, BUCKET, "prefix")
        key = manifest_key("prefix/net/terraform.tfplan")
        store.write(key, manifest())
        index = s3.get_object(Bucket=BUCKET, Key="prefix/_tfworker/pending/net/p1")
        assert index["Body"].read().decode() == key
        assert store.keys_for("net") == [key]

        tag = store.read(key)[1]
        store.write(key, manifest(status="stale"), if_match=tag)
        listed = s3.list_objects_v2(Bucket=BUCKET, Prefix="prefix/_tfworker/pending/")
        assert "Contents" not in listed
        assert store.keys_for("net") == []

    def test_previews_and_local_plans_are_not_indexed(self, s3):
        store = S3ManifestStore(s3, BUCKET, "prefix")
        store.write("prefix/a.manifest.json", manifest(storage="preview"))
        assert store.keys_for("net") == []

    def test_keys_for_prunes_entries_that_no_longer_apply(self, s3):
        store = S3ManifestStore(s3, BUCKET, "prefix")
        key = manifest_key("prefix/net/terraform.tfplan")
        store.write(key, manifest(plan_id="old"))
        # a newer plan at the same key leaves the old entry pointing at it
        store.write(key, manifest(plan_id="new"))
        s3.put_object(
            Bucket=BUCKET, Key="prefix/_tfworker/pending/net/gone", Body=b"prefix/gone"
        )
        assert store.keys_for("net") == [key]
        listed = s3.list_objects_v2(Bucket=BUCKET, Prefix="prefix/_tfworker/pending/")
        assert [o["Key"] for o in listed["Contents"]] == [
            "prefix/_tfworker/pending/net/new"
        ]

    def test_pointer(self, s3):
        store = S3ManifestStore(s3, BUCKET, "prefix")
        assert store.current("prefix/net/terraform.tfplan") is None
        key = store.record_key("net", "p1")
        assert key == "prefix/_tfworker/plans/net/p1.json"
        store.point("prefix/net/terraform.tfplan", key)
        assert store.current("prefix/net/terraform.tfplan") == key

    def test_retire_removes_the_plan_and_log(self, s3):
        store = S3ManifestStore(s3, BUCKET, "prefix")
        plan = "prefix/net/terraform.tfplan"
        for k in (plan, "prefix/net/terraform.log"):
            s3.put_object(Bucket=BUCKET, Key=k, Body=b"x")
        key = store.record_key("net", "p1")
        store.point(plan, key)
        store.retire(key, manifest(plan_key=plan))
        assert [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]] == [
            manifest_key(plan)
        ]

    def test_retire_leaves_a_newer_plan_alone(self, s3):
        store = S3ManifestStore(s3, BUCKET, "prefix")
        plan = "prefix/net/terraform.tfplan"
        s3.put_object(Bucket=BUCKET, Key=plan, Body=b"newer")
        store.point(plan, store.record_key("net", "newer"))
        store.retire(store.record_key("net", "old"), manifest(plan_key=plan))
        assert s3.get_object(Bucket=BUCKET, Key=plan)["Body"].read() == b"newer"

    def test_retire_failure_is_logged(self, s3, monkeypatch):
        store = S3ManifestStore(s3, BUCKET, "prefix")

        def fail(**kwargs):
            raise botocore.exceptions.ClientError({"Error": {"Code": "500"}}, "op")

        monkeypatch.setattr(s3, "delete_object", fail)
        store.retire("k", manifest(plan_key="prefix/net/terraform.tfplan"))

    def test_unreadable_index_entries_are_skipped(self, s3, monkeypatch):
        store = S3ManifestStore(s3, BUCKET, "prefix")
        good = manifest_key("prefix/net/terraform.tfplan")
        store.write(good, manifest(plan_id="good"))
        s3.put_object(
            Bucket=BUCKET, Key="prefix/_tfworker/pending/net/bad", Body=b"prefix/bad"
        )
        real_get = s3.get_object

        def get(**kwargs):
            if kwargs["Key"].endswith("/bad"):
                raise botocore.exceptions.ClientError(
                    {"Error": {"Code": "AccessDenied"}}, "GetObject"
                )
            return real_get(**kwargs)

        monkeypatch.setattr(s3, "get_object", get)
        assert store.keys_for("net") == [good]

    def test_index_entry_deleted_by_another_run(self, s3, monkeypatch):
        store = S3ManifestStore(s3, BUCKET, "prefix")
        s3.put_object(Bucket=BUCKET, Key="prefix/_tfworker/pending/net/gone", Body=b"x")
        real_get = s3.get_object

        def get(**kwargs):
            if kwargs["Key"].endswith("/gone"):
                raise botocore.exceptions.ClientError(
                    {"Error": {"Code": "NoSuchKey"}}, "GetObject"
                )
            return real_get(**kwargs)

        monkeypatch.setattr(s3, "get_object", get)
        assert store.keys_for("net") == []

    def test_conditional_writes(self, s3):
        store = S3ManifestStore(s3, BUCKET, "prefix")
        store.write("prefix/m", manifest(), create_only=True)
        with pytest.raises(ReportConflictError):
            store.write("prefix/m", manifest(), create_only=True)
        with pytest.raises(ReportConflictError):
            store.write("prefix/m", manifest(), if_match='"nope"')

    def test_unreadable_manifest(self, s3):
        s3.put_object(Bucket=BUCKET, Key="prefix/m", Body=b"{}")
        found, tag = S3ManifestStore(s3, BUCKET, "prefix").read("prefix/m")
        assert found is None
        assert tag

    @pytest.mark.parametrize(
        "error",
        [
            botocore.exceptions.ClientError({"Error": {"Code": "500"}}, "op"),
            botocore.exceptions.EndpointConnectionError(endpoint_url="x"),
        ],
    )
    def test_errors_are_backend_errors(self, s3, error, monkeypatch):
        store = S3ManifestStore(s3, BUCKET, "prefix")

        def fail(*args, **kwargs):
            raise error

        monkeypatch.setattr(s3, "get_object", fail)
        monkeypatch.setattr(s3, "put_object", fail)
        monkeypatch.setattr(s3, "get_paginator", fail)
        with pytest.raises(BackendError):
            store.read("prefix/m")
        with pytest.raises(BackendError):
            store.write("prefix/m", manifest())
        with pytest.raises(BackendError):
            store.keys_for("net")
        with pytest.raises(BackendError):
            store.current("prefix/net/terraform.tfplan")
        with pytest.raises(BackendError):
            store.point("prefix/net/terraform.tfplan", "k")

    def test_index_failure_is_logged_not_raised(self, s3, monkeypatch):
        store = S3ManifestStore(s3, BUCKET, "prefix")
        real_put = s3.put_object

        def put(**kwargs):
            if "_tfworker/pending/" in kwargs["Key"]:
                raise botocore.exceptions.ClientError(
                    {"Error": {"Code": "AccessDenied"}}, "PutObject"
                )
            return real_put(**kwargs)

        monkeypatch.setattr(s3, "put_object", put)
        store.write("prefix/m", manifest())
        assert store.read("prefix/m")[0] == manifest()


class FlakyStore(ManifestStore):
    """A store whose conditional writes lose a set number of races."""

    def __init__(self, current, conflicts):
        self.current = current
        self.conflicts = conflicts
        self.writes = 0

    def read(self, key):
        return (self.current, "tag") if self.current else None

    def write(self, key, manifest, if_match=None, create_only=False):
        self.writes += 1
        if self.conflicts:
            self.conflicts -= 1
            raise ReportConflictError("raced")
        self.current = manifest


class TestMove:
    def test_moves(self):
        store = FlakyStore(manifest(), conflicts=0)
        moved = move(store, "k", "applying", T0, run_id="r2", reason="go")
        assert moved.status == "applying"
        assert store.current.history[-1].reason == "go"

    def test_missing_or_unreadable(self):
        assert move(FlakyStore(None, 0), "k", "stale", T0) is None

    def test_wrong_plan_id(self):
        assert move(FlakyStore(manifest(), 0), "k", "stale", T0, plan_id="x") is None

    def test_unexpected_status(self):
        store = FlakyStore(manifest(status="applying"), 0)
        assert move(store, "k", "failed", T0, expect={"pending"}) is None

    def test_invalid_move(self):
        assert move(FlakyStore(manifest(), 0), "k", "applied", T0) is None

    def test_retries_a_lost_race(self):
        store = FlakyStore(manifest(), conflicts=1)
        assert move(store, "k", "stale", T0).status == "stale"
        assert store.writes == 2

    def test_gives_up(self):
        store = FlakyStore(manifest(), conflicts=5)
        assert move(store, "k", "stale", T0, retries=2) is None


class TestMarkPendingStale:
    def test_marks_others_stale(self, s3):
        store = S3ManifestStore(s3, BUCKET, "prefix")
        keep = manifest_key("prefix/r1/net/terraform.tfplan")
        other = manifest_key("prefix/r2/net/terraform.tfplan")
        store.write(keep, manifest(plan_id="a"))
        store.write(other, manifest(plan_id="b"))
        store.write(manifest_key("prefix/x/dns/terraform.tfplan"), manifest("c", "dns"))
        assert mark_pending_stale(store, "net", T0, "r3", "applied", keep=keep) == 1
        assert store.read(other)[0].status == "stale"
        assert store.read(other)[0].history[-1].reason == "applied"
        assert store.read(keep)[0].status == "pending"

    def test_listing_failure(self):
        class Broken(FlakyStore):
            def keys_for(self, definition):
                raise BackendError("no")

        assert mark_pending_stale(Broken(None, 0), "net", T0, "r", "x") == 0

    def test_move_failure_continues(self):
        class Broken(FlakyStore):
            def keys_for(self, definition):
                return ["a", "b"]

            def read(self, key):
                raise BackendError("no")

        assert mark_pending_stale(Broken(None, 0), "net", T0, "r", "x") == 0
