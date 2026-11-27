"""The S3 handler's plan manifests and apply gate, against moto."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
from zipfile import ZipFile

import boto3
import pytest
from moto import mock_aws

from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.exceptions import BackendError, HandlerError, PlanRefusedError
from tfworker.handlers.s3 import S3Handler
from tfworker.report import BackendLocation
from tfworker.util.plan_manifest import S3ManifestStore, manifest_key, sha256_file

STATE = {"lineage": "lin", "serial": 4}


def make_plan(path: Path, lineage="lin", serial=4, content="v1") -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr("tfstate", json.dumps({"lineage": lineage, "serial": serial}))
        archive.writestr("content", content)
    return path


def definition(tmp_path: Path, name="net") -> MagicMock:
    d = MagicMock()
    d.name = name
    d.plan_file = str(tmp_path / f"{name}.tfplan")
    return d


def result(code=2) -> MagicMock:
    r = MagicMock()
    r.exit_code = code
    r.log_file = MagicMock(side_effect=lambda p: Path(p).write_text("log"))
    return r


@pytest.fixture
def env(mock_app_state):
    mock_app_state.root_options.backend_plans = True
    mock_app_state.root_options.run_id = "run-a"
    mock_app_state.backend = MagicMock()
    mock_app_state.backend.location.return_value = BackendLocation(
        backend="s3", bucket="test-bucket", state_key="prefix/net/terraform.tfstate"
    )
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")
        s3.put_object(
            Bucket="test-bucket",
            Key="prefix/net/terraform.tfstate",
            Body=json.dumps(STATE).encode(),
        )
        yield mock_app_state, s3


def handler(app_state) -> S3Handler:
    h = S3Handler()
    h._app_state = app_state
    return h


def save_plan(app_state, tmp_path, name="net", content="v1", **plan) -> MagicMock:
    """Plan and upload as a planning run would; returns the definition."""
    d = definition(tmp_path / "planner", name)
    Path(d.plan_file).parent.mkdir(parents=True, exist_ok=True)
    make_plan(Path(d.plan_file), content=content, **plan)
    handler(app_state)._post_plan(definition=d, result=result())
    return d


def stored(s3, key):
    return S3ManifestStore(s3, "test-bucket", "prefix").read(manifest_key(key))[0]


KEY = "prefix/run-a/net/terraform.tfplan"


class TestManifestWritten:
    def test_post_plan_writes_a_pending_manifest(self, env, tmp_path):
        app_state, s3 = env
        d = save_plan(app_state, tmp_path)
        m = stored(s3, KEY)
        assert m.status == "pending"
        assert m.storage == "backend"
        assert m.run_id == "run-a"
        assert m.definition == "net"
        assert m.plan_key == KEY
        assert m.sha256 == sha256_file(Path(d.plan_file))
        assert (m.base_lineage, m.base_serial) == ("lin", 4)
        assert m.state.state_key == "prefix/net/terraform.tfstate"
        assert m.expires_at == m.created_at + timedelta(days=7)
        assert m.base_state_version_id is None
        index = s3.list_objects_v2(Bucket="test-bucket", Prefix="prefix/_tfworker/")
        assert [o["Key"] for o in index["Contents"]] == [
            f"prefix/_tfworker/pending/net/{m.plan_id}"
        ]

    def test_versioned_state_is_recorded(self, env, tmp_path):
        app_state, s3 = env
        s3.put_bucket_versioning(
            Bucket="test-bucket", VersioningConfiguration={"Status": "Enabled"}
        )
        put = s3.put_object(
            Bucket="test-bucket",
            Key="prefix/net/terraform.tfstate",
            Body=json.dumps(STATE).encode(),
        )
        save_plan(app_state, tmp_path)
        assert stored(s3, KEY).base_state_version_id == put["VersionId"]

    def test_unrecognized_location_is_left_out(self, env, tmp_path):
        app_state, s3 = env
        app_state.backend.location.side_effect = BackendError("no")
        save_plan(app_state, tmp_path)
        assert stored(s3, KEY).state is None

    def test_no_changes_writes_nothing(self, env, tmp_path):
        app_state, s3 = env
        d = definition(tmp_path)
        make_plan(Path(d.plan_file))
        handler(app_state)._post_plan(definition=d, result=result(0))
        assert "Contents" not in s3.list_objects_v2(
            Bucket="test-bucket", Prefix="prefix/run-a"
        )

    def test_a_new_plan_supersedes_the_pending_one(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        first = stored(s3, KEY)
        app_state.terraform_options.destroy = True
        save_plan(app_state, tmp_path / "again", content="v2")
        second = stored(s3, KEY)
        assert second.plan_id != first.plan_id
        assert second.status == "pending"
        assert second.destroy is True

    def test_manifest_write_failure_fails_the_upload(self, env, tmp_path, monkeypatch):
        app_state, _ = env
        h = handler(app_state)

        def fail(*args, **kwargs):
            raise BackendError("denied")

        monkeypatch.setattr(h.store, "write", fail)
        d = definition(tmp_path)
        make_plan(Path(d.plan_file))
        with pytest.raises(HandlerError, match="plan manifest"):
            h._post_plan(definition=d, result=result())


class TestHasPlan:
    def test_pending_plan(self, env, tmp_path):
        app_state, _ = env
        save_plan(app_state, tmp_path)
        assert handler(app_state).has_plan(definition(tmp_path)) is True

    def test_plan_without_manifest_falls_back(self, env, tmp_path):
        app_state, s3 = env
        s3.put_object(Bucket="test-bucket", Key=KEY, Body=b"legacy")
        assert handler(app_state).has_plan(definition(tmp_path)) is True

    def test_plan_without_manifest_when_required(self, env, tmp_path):
        app_state, s3 = env
        app_state.terraform_options.require_plan_manifest = True
        s3.put_object(Bucket="test-bucket", Key=KEY, Body=b"legacy")
        assert handler(app_state).has_plan(definition(tmp_path)) is False

    def test_unreadable_manifest(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        s3.put_object(Bucket="test-bucket", Key=manifest_key(KEY), Body=b"{}")
        assert handler(app_state).has_plan(definition(tmp_path)) is False

    def test_plan_no_longer_pending(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        h = handler(app_state)
        h._move(KEY, "discarded", reason="vetoed")
        assert h.has_plan(definition(tmp_path)) is False

    def test_expired_plan_is_marked_expired(self, env, tmp_path, monkeypatch):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        h = handler(app_state)
        later = datetime.now(timezone.utc) + timedelta(days=8)
        monkeypatch.setattr(h, "_now", lambda: later)
        assert h.has_plan(definition(tmp_path)) is False
        assert stored(s3, KEY).status == "expired"

    def test_manifest_read_error(self, env, tmp_path, monkeypatch):
        app_state, s3 = env
        s3.put_object(Bucket="test-bucket", Key=KEY, Body=b"plan")
        h = handler(app_state)

        def fail(key):
            raise BackendError("denied")

        monkeypatch.setattr(h.store, "read", fail)
        with pytest.raises(HandlerError, match="plan manifest"):
            h.has_plan(definition(tmp_path))


class TestGate:
    def test_pending_plan_is_downloaded_and_taken(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        app_state.root_options.run_id = "run-a"
        h = handler(app_state)
        d = definition(tmp_path)
        assert h.get_plan(d) is True
        assert stored(s3, KEY).status == "pending"

        h._pre_apply(definition=d)
        taken = stored(s3, KEY)
        assert taken.status == "applying"
        assert taken.history[-1].run_id == "run-a"
        listed = s3.list_objects_v2(Bucket="test-bucket", Prefix="prefix/run-a/")
        assert [o["Key"] for o in listed["Contents"]] == [manifest_key(KEY)]

        h._post_apply(definition=d)
        assert stored(s3, KEY).status == "applied"

    def test_concurrent_take_has_one_winner(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        first, second = handler(app_state), handler(app_state)
        d1, d2 = definition(tmp_path / "one"), definition(tmp_path / "two")
        for d in (d1, d2):
            Path(d.plan_file).parent.mkdir()
        assert first.get_plan(d1) is True
        assert second.get_plan(d2) is True

        first._pre_apply(definition=d1)
        with pytest.raises(PlanRefusedError, match="no longer pending"):
            second._pre_apply(definition=d2)
        assert stored(s3, KEY).status == "applying"

    def test_altered_plan_is_discarded(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        other = make_plan(tmp_path / "other.tfplan", content="tampered")
        s3.put_object(Bucket="test-bucket", Key=KEY, Body=other.read_bytes())
        d = definition(tmp_path)
        assert handler(app_state).get_plan(d) is False
        assert not Path(d.plan_file).exists()
        m = stored(s3, KEY)
        assert m.status == "discarded"
        assert "sha256" in m.history[-1].reason

    def test_plan_expiring_between_check_and_download(self, env, tmp_path, monkeypatch):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        h = handler(app_state)
        later = datetime.now(timezone.utc) + timedelta(days=8)
        monkeypatch.setattr(h, "_now", lambda: later)
        assert h.get_plan(definition(tmp_path)) is False
        assert stored(s3, KEY).status == "expired"

    def test_plan_not_pending_is_not_downloaded(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        h = handler(app_state)
        h._move(KEY, "discarded")
        d = definition(tmp_path)
        assert h.get_plan(d) is False
        assert not Path(d.plan_file).exists()

    def test_unreadable_manifest_is_not_used(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        s3.put_object(Bucket="test-bucket", Key=manifest_key(KEY), Body=b"{}")
        assert handler(app_state).get_plan(definition(tmp_path)) is False

    def test_state_changed_marks_the_plan_stale(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        s3.put_object(
            Bucket="test-bucket",
            Key="prefix/net/terraform.tfstate",
            Body=json.dumps({"lineage": "lin", "serial": 5}).encode(),
        )
        assert handler(app_state).get_plan(definition(tmp_path)) is False
        assert stored(s3, KEY).status == "stale"

    def test_plan_without_manifest_falls_back(self, env, tmp_path):
        app_state, s3 = env
        plan = make_plan(tmp_path / "legacy.tfplan")
        s3.put_object(Bucket="test-bucket", Key=KEY, Body=plan.read_bytes())
        h = handler(app_state)
        d = definition(tmp_path)
        assert h.get_plan(d) is True
        h._pre_apply(definition=d)
        h._post_apply(definition=d)
        assert stored_or_none(s3, KEY) is None

    def test_plan_without_manifest_when_required(self, env, tmp_path):
        app_state, s3 = env
        app_state.terraform_options.require_plan_manifest = True
        plan = make_plan(tmp_path / "legacy.tfplan")
        s3.put_object(Bucket="test-bucket", Key=KEY, Body=plan.read_bytes())
        d = definition(tmp_path)
        assert handler(app_state).get_plan(d) is False
        assert not Path(d.plan_file).exists()

    def test_local_file_swapped_before_apply_is_refused(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        h = handler(app_state)
        d = definition(tmp_path)
        assert h.get_plan(d) is True
        make_plan(Path(d.plan_file), content="swapped")
        with pytest.raises(PlanRefusedError, match="sha256"):
            h._pre_apply(definition=d)
        assert stored(s3, KEY).status == "pending"


def stored_or_none(s3, key):
    found = S3ManifestStore(s3, "test-bucket", "prefix").read(manifest_key(key))
    return found[0] if found else None


class TestSameRunPlanAndApply:
    def test_plan_made_by_this_run_is_taken_and_applied(self, env, tmp_path):
        app_state, s3 = env
        h = handler(app_state)
        d = definition(tmp_path)
        make_plan(Path(d.plan_file))
        h._post_plan(definition=d, result=result())
        h._pre_apply(definition=d)
        assert stored(s3, KEY).status == "applying"
        h._apply_failed(definition=d)
        m = stored(s3, KEY)
        assert m.status == "failed"
        assert [c.status for c in m.history] == ["applying", "failed"]

    def test_refusal_error_stage_leaves_the_plan_alone(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        h = handler(app_state)
        h._apply_failed(definition=definition(tmp_path))
        assert stored(s3, KEY).status == "pending"

    def test_teardown_fails_plans_left_applying(self, env, tmp_path):
        app_state, s3 = env
        h = handler(app_state)
        d = definition(tmp_path)
        make_plan(Path(d.plan_file))
        h._post_plan(definition=d, result=result())
        h._pre_apply(definition=d)
        h.teardown("dep", str(tmp_path))
        m = stored(s3, KEY)
        assert m.status == "failed"
        assert "ended before" in m.history[-1].reason
        h.teardown("dep", str(tmp_path))


class TestEagerStaleness:
    def test_apply_marks_other_pending_plans_stale(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path / "a")
        app_state.root_options.run_id = "run-b"
        save_plan(app_state, tmp_path / "b")
        app_state.root_options.run_id = "run-c"
        save_plan(app_state, tmp_path / "c", name="dns")

        app_state.root_options.run_id = "run-a"
        h = handler(app_state)
        d = definition(tmp_path)
        assert h.get_plan(d) is True
        h._pre_apply(definition=d)
        h._post_apply(definition=d)

        assert stored(s3, KEY).status == "applied"
        other = stored(s3, "prefix/run-b/net/terraform.tfplan")
        assert other.status == "stale"
        assert "run-a" in other.history[-1].reason
        assert stored(s3, "prefix/run-c/dns/terraform.tfplan").status == "pending"


class TestForcePlan:
    def test_forced_plan_supersedes_the_saved_plan(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        app_state.terraform_options.force_plan = True
        handler(app_state)._check_plan("dep", definition(tmp_path))
        m = stored(s3, KEY)
        assert m.status == "superseded"
        assert "forced plan" in m.history[-1].reason
        assert "Contents" not in s3.list_objects_v2(
            Bucket="test-bucket", Prefix="prefix/_tfworker/pending/"
        )


class TestPreview:
    def test_previews_are_stored_apart_and_never_offered(self, env, tmp_path):
        app_state, s3 = env
        app_state.terraform_options.intent = "preview"
        h = handler(app_state)
        key = h.get_remote_file("net")
        assert key == "prefix/_tfworker/previews/run-a/net/terraform.tfplan"
        save_plan(app_state, tmp_path)
        m = stored(s3, key)
        assert m.storage == "preview"
        assert h.has_plan(definition(tmp_path)) is False
        assert "Contents" not in s3.list_objects_v2(
            Bucket="test-bucket", Prefix="prefix/_tfworker/pending/"
        )

    def test_preview_run_does_not_fetch_saved_plans(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        app_state.terraform_options.intent = "preview"
        d = definition(tmp_path)
        handler(app_state)._check_plan("dep", d)
        assert not Path(d.plan_file).exists()


class TestStages:
    def test_apply_and_destroy_stages(self):
        h = S3Handler()
        for action in (TerraformAction.APPLY, TerraformAction.DESTROY):
            stages = h.execution_functions[action]
            assert stages[TerraformStage.POST] == h._post_apply
            assert stages[TerraformStage.ERROR] == h._apply_failed

    def test_store_errors_while_moving_are_logged(self, env, monkeypatch):
        app_state, _ = env
        h = handler(app_state)

        def fail(key):
            raise BackendError("denied")

        monkeypatch.setattr(h.store, "read", fail)
        assert h._move(KEY, "stale") is None
