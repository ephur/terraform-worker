"""The S3 handler's plan manifests and apply gate, against moto."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

import boto3
import pytest
from moto import mock_aws

from tfworker.commands.terraform import TerraformResult
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.exceptions import BackendError, HandlerError, PlanRefusedError
from tfworker.handlers.s3 import S3Handler
from tfworker.report import BackendLocation
from tfworker.util.plan_manifest import S3ManifestStore, manifest_key, sha256_file

STATE = {"lineage": "lin", "serial": 4}
BUCKET = "test-bucket"
KEY = "prefix/run-a/net/terraform.tfplan"


def make_plan(path: Path, lineage="lin", serial=4, content="v1") -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr("tfstate", json.dumps({"lineage": lineage, "serial": serial}))
        archive.writestr("content", content)
    return path


def definition(tmp_path: Path, name="net") -> MagicMock:
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    d = MagicMock()
    d.name = name
    d.plan_file = str(tmp_path / f"{name}.tfplan")
    return d


def result(code=2) -> MagicMock:
    r = MagicMock()
    r.exit_code = code
    r.outside_terraform = False
    r.log_file = MagicMock(side_effect=lambda p: Path(p).write_text("log"))
    return r


@pytest.fixture
def env(mock_app_state):
    mock_app_state.root_options.backend_plans = True
    mock_app_state.root_options.run_id = "run-a"
    mock_app_state.terraform_options.plan_destroy = False
    mock_app_state.backend = MagicMock()
    mock_app_state.backend.location.return_value = BackendLocation(
        backend="s3", bucket=BUCKET, state_key="prefix/net/terraform.tfstate"
    )
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        s3.put_object(
            Bucket=BUCKET,
            Key="prefix/net/terraform.tfstate",
            Body=json.dumps(STATE).encode(),
        )
        yield mock_app_state, s3


def handler(app_state) -> S3Handler:
    h = S3Handler()
    h._app_state = app_state
    return h


def save_plan(app_state, tmp_path, name="net", content="v1", **plan) -> MagicMock:
    """Plan and upload as a planning run would; returns the planner's definition."""
    d = definition(tmp_path / "planner", name)
    make_plan(Path(d.plan_file), content=content, **plan)
    handler(app_state)._post_plan(definition=d, result=result())
    return d


def store(s3) -> S3ManifestStore:
    return S3ManifestStore(s3, BUCKET, "prefix")


def record(s3, slot=KEY) -> str:
    return store(s3).current(slot)


def stored(s3, slot=KEY):
    return store(s3).read(record(s3, slot))[0]


def keys(s3, prefix):
    return [
        o["Key"]
        for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", [])
    ]


class TestManifestWritten:
    def test_post_plan_writes_a_pending_manifest(self, env, tmp_path):
        app_state, s3 = env
        d = save_plan(app_state, tmp_path)
        m = stored(s3)
        assert record(s3) == f"prefix/_tfworker/plans/net/{m.plan_id}.json"
        assert m.status == "pending"
        assert m.storage == "backend"
        assert m.run_id == "run-a"
        assert m.plan_key == KEY
        assert m.destroy is False
        assert m.sha256 == sha256_file(Path(d.plan_file))
        assert (m.base_lineage, m.base_serial) == ("lin", 4)
        assert m.state.state_key == "prefix/net/terraform.tfstate"
        assert m.expires_at == m.created_at + timedelta(days=7)
        assert keys(s3, "prefix/_tfworker/pending/") == [
            f"prefix/_tfworker/pending/net/{m.plan_id}"
        ]

    def test_version_is_read_before_the_plan(self, env, tmp_path):
        app_state, s3 = env
        s3.put_bucket_versioning(
            Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
        )
        before = s3.put_object(
            Bucket=BUCKET,
            Key="prefix/net/terraform.tfstate",
            Body=json.dumps(STATE).encode(),
        )["VersionId"]
        h = handler(app_state)
        d = definition(tmp_path)
        h._check_plan("dep", d)
        s3.put_object(Bucket=BUCKET, Key="prefix/net/terraform.tfstate", Body=b"{}")
        make_plan(Path(d.plan_file))
        h._post_plan(definition=d, result=result())
        assert stored(s3).base_state_version_id == before

    @pytest.mark.parametrize("flag", ["destroy", "plan_destroy"])
    def test_destroy_plans_are_marked(self, env, tmp_path, flag):
        app_state, s3 = env
        setattr(app_state.terraform_options, flag, True)
        save_plan(app_state, tmp_path)
        assert stored(s3).destroy is True

    def test_unrecognized_location_is_left_out(self, env, tmp_path):
        app_state, s3 = env
        app_state.backend.location.side_effect = BackendError("no")
        save_plan(app_state, tmp_path)
        assert stored(s3).state is None

    def test_no_changes_writes_nothing(self, env, tmp_path):
        app_state, s3 = env
        d = definition(tmp_path)
        make_plan(Path(d.plan_file))
        handler(app_state)._post_plan(definition=d, result=result(0))
        assert keys(s3, "prefix/run-a") == []

    def test_a_new_plan_supersedes_and_keeps_the_old_record(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        first_key = record(s3)
        save_plan(app_state, tmp_path / "again", content="v2")
        assert record(s3) != first_key
        old = store(s3).read(first_key)[0]
        assert old.status == "superseded"
        assert "replaced by a plan" in old.history[-1].reason
        assert stored(s3).status == "pending"
        plan = tmp_path / "downloaded.tfplan"
        s3.download_file(BUCKET, KEY, str(plan))
        assert sha256_file(plan) == stored(s3).sha256

    def test_manifest_failure_leaves_no_plan_behind(self, env, tmp_path, monkeypatch):
        app_state, s3 = env
        h = handler(app_state)

        def fail(*args, **kwargs):
            raise BackendError("denied")

        monkeypatch.setattr(h.store, "write", fail)
        d = definition(tmp_path)
        make_plan(Path(d.plan_file))
        with pytest.raises(HandlerError, match="plan manifest"):
            h._post_plan(definition=d, result=result())
        assert keys(s3, "prefix/run-a") == []

    def test_upload_failure_is_a_handler_error(self, env, tmp_path, monkeypatch):
        app_state, _ = env
        h = handler(app_state)

        def fail(*args, **kwargs):
            raise RuntimeError("disk")

        monkeypatch.setattr(h, "_s3_put_plan", fail)
        d = definition(tmp_path)
        make_plan(Path(d.plan_file))
        with pytest.raises(HandlerError, match="uploading planfile"):
            h._post_plan(definition=d, result=result())


class TestHasPlan:
    def test_pending_plan(self, env, tmp_path):
        app_state, _ = env
        save_plan(app_state, tmp_path)
        assert handler(app_state).has_plan(definition(tmp_path)) is True

    def test_plan_without_manifest_falls_back(self, env, tmp_path):
        app_state, s3 = env
        s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"legacy")
        assert handler(app_state).has_plan(definition(tmp_path)) is True

    def test_plan_without_manifest_when_required(self, env, tmp_path):
        app_state, s3 = env
        app_state.terraform_options.require_plan_manifest = True
        s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"legacy")
        assert handler(app_state).has_plan(definition(tmp_path)) is False

    def test_unreadable_manifest(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        s3.put_object(Bucket=BUCKET, Key=record(s3), Body=b"{}")
        assert handler(app_state).has_plan(definition(tmp_path)) is False

    def test_pointer_to_a_missing_manifest(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        s3.delete_object(Bucket=BUCKET, Key=record(s3))
        assert handler(app_state).has_plan(definition(tmp_path)) is False

    def test_plan_no_longer_pending(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        h = handler(app_state)
        h._move(record(s3), "discarded", reason="vetoed")
        assert h.has_plan(definition(tmp_path)) is False

    def test_expired_plan_is_retired(self, env, tmp_path, monkeypatch):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        h = handler(app_state)
        later = datetime.now(timezone.utc) + timedelta(days=8)
        monkeypatch.setattr(h, "_now", lambda: later)
        assert h.has_plan(definition(tmp_path)) is False
        assert stored(s3).status == "expired"
        assert keys(s3, "prefix/run-a/") == [manifest_key(KEY)]

    def test_manifest_read_error_is_not_fatal(self, env, tmp_path, monkeypatch):
        app_state, s3 = env
        s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"plan")
        h = handler(app_state)

        def fail(key):
            raise BackendError("throttled")

        monkeypatch.setattr(h.store, "current", fail)
        assert h.has_plan(definition(tmp_path)) is False


class TestGate:
    def test_pending_plan_is_downloaded_taken_and_applied(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        h = handler(app_state)
        d = definition(tmp_path)
        assert h.get_plan(d) is True
        assert stored(s3).status == "pending"

        h._pre_apply(definition=d)
        taken = stored(s3)
        assert taken.status == "applying"
        assert taken.history[-1].run_id == "run-a"

        h._post_apply(definition=d)
        assert stored(s3).status == "applied"
        assert keys(s3, "prefix/run-a/") == [manifest_key(KEY)]

    def test_concurrent_take_has_one_winner(self, env, tmp_path):
        """Both runs read the manifest as pending before either writes."""
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        first, second = handler(app_state), handler(app_state)
        d1, d2 = definition(tmp_path / "one"), definition(tmp_path / "two")
        assert first.get_plan(d1) is True
        assert second.get_plan(d2) is True

        stale_read = second.store.read(record(s3))
        real_read = second.store.read
        reads = []

        def read_once_stale(key):
            reads.append(key)
            return stale_read if len(reads) == 1 else real_read(key)

        first._pre_apply(definition=d1)
        with patch.object(second.store, "read", side_effect=read_once_stale):
            with pytest.raises(PlanRefusedError, match="no longer pending"):
                second._pre_apply(definition=d2)
        assert len(reads) == 2
        assert [c.status for c in stored(s3).history] == ["applying"]

    def test_altered_plan_is_discarded_and_removed(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        other = make_plan(tmp_path / "other.tfplan", content="tampered")
        s3.put_object(Bucket=BUCKET, Key=KEY, Body=other.read_bytes())
        d = definition(tmp_path)
        assert handler(app_state).get_plan(d) is False
        assert not Path(d.plan_file).exists()
        m = stored(s3)
        assert m.status == "discarded"
        assert "sha256" in m.history[-1].reason
        assert keys(s3, "prefix/run-a/") == [manifest_key(KEY)]

    def test_plan_expiring_between_check_and_download(self, env, tmp_path, monkeypatch):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        h = handler(app_state)
        later = datetime.now(timezone.utc) + timedelta(days=8)
        monkeypatch.setattr(h, "_now", lambda: later)
        assert h.get_plan(definition(tmp_path)) is False
        assert stored(s3).status == "expired"

    def test_plan_not_pending_is_not_downloaded(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        h = handler(app_state)
        h._move(record(s3), "discarded")
        d = definition(tmp_path)
        assert h.get_plan(d) is False
        assert not Path(d.plan_file).exists()

    def test_unreadable_manifest_is_not_used(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        s3.put_object(Bucket=BUCKET, Key=record(s3), Body=b"{}")
        assert handler(app_state).get_plan(definition(tmp_path)) is False

    def test_state_changed_marks_the_plan_stale(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        s3.put_object(
            Bucket=BUCKET,
            Key="prefix/net/terraform.tfstate",
            Body=json.dumps({"lineage": "lin", "serial": 5}).encode(),
        )
        assert handler(app_state).get_plan(definition(tmp_path)) is False
        assert stored(s3).status == "stale"

    def test_no_saved_plan_warns_about_nothing(self, env, tmp_path):
        app_state, _ = env
        with patch("tfworker.handlers.s3.log.warn") as warn:
            handler(app_state)._check_plan("dep", definition(tmp_path))
        warn.assert_not_called()

    def test_plan_without_manifest_falls_back(self, env, tmp_path):
        app_state, s3 = env
        plan = make_plan(tmp_path / "legacy.tfplan")
        s3.put_object(Bucket=BUCKET, Key=KEY, Body=plan.read_bytes())
        h = handler(app_state)
        d = definition(tmp_path)
        with patch("tfworker.handlers.s3.log.warn") as warn:
            assert h.get_plan(d) is True
        assert "older worker" in warn.call_args.args[0]
        h._pre_apply(definition=d)
        assert keys(s3, "prefix/run-a/") == []
        h._post_apply(definition=d)

    def test_plan_without_manifest_when_required(self, env, tmp_path):
        app_state, s3 = env
        app_state.terraform_options.require_plan_manifest = True
        plan = make_plan(tmp_path / "legacy.tfplan")
        s3.put_object(Bucket=BUCKET, Key=KEY, Body=plan.read_bytes())
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
        assert stored(s3).status == "pending"

    def test_destroy_plan_is_refused_by_an_apply(self, env, tmp_path):
        app_state, s3 = env
        app_state.terraform_options.plan_destroy = True
        save_plan(app_state, tmp_path)
        app_state.terraform_options.plan_destroy = False
        h = handler(app_state)
        d = definition(tmp_path)
        assert h.get_plan(d) is True
        with pytest.raises(PlanRefusedError, match="is a destroy plan"):
            h._pre_apply(definition=d)
        h2 = handler(app_state)
        Path(d.plan_file).unlink()
        assert h2.get_plan(d) is True
        h2._pre_destroy(definition=d)
        assert stored(s3).status == "applying"

    def test_apply_plan_is_refused_by_a_destroy(self, env, tmp_path):
        app_state, _ = env
        save_plan(app_state, tmp_path)
        h = handler(app_state)
        d = definition(tmp_path)
        assert h.get_plan(d) is True
        with pytest.raises(PlanRefusedError, match="not a destroy plan"):
            h._pre_destroy(definition=d)

    def test_expired_at_apply_time(self, env, tmp_path, monkeypatch):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        h = handler(app_state)
        d = definition(tmp_path)
        assert h.get_plan(d) is True
        later = datetime.now(timezone.utc) + timedelta(days=8)
        monkeypatch.setattr(h, "_now", lambda: later)
        with pytest.raises(PlanRefusedError, match="expired"):
            h._pre_apply(definition=d)
        assert stored(s3).status == "expired"

    def test_store_error_while_taking(self, env, tmp_path, monkeypatch):
        app_state, _ = env
        save_plan(app_state, tmp_path)
        h = handler(app_state)
        d = definition(tmp_path)
        assert h.get_plan(d) is True

        def fail(*args, **kwargs):
            raise BackendError("throttled")

        monkeypatch.setattr(h.store, "write", fail)
        with pytest.raises(PlanRefusedError, match="unable to take it: .*throttled"):
            h._pre_apply(definition=d)


class TestPlanFileWithBackendPlans:
    """`--plan-file-path` keeps a copy of a backend plan on disk."""

    def local_copy(self, app_state, tmp_path, content="v1"):
        planner = save_plan(app_state, tmp_path)
        d = definition(tmp_path / "applier")
        Path(d.plan_file).write_bytes(Path(planner.plan_file).read_bytes())
        if content != "v1":
            make_plan(Path(d.plan_file), content=content)
        return d

    def test_copy_on_disk_is_gated_and_taken(self, env, tmp_path):
        app_state, s3 = env
        d = self.local_copy(app_state, tmp_path)
        h = handler(app_state)
        h._pre_apply(definition=d)
        assert stored(s3).status == "applying"
        h._post_apply(definition=d)
        assert stored(s3).status == "applied"

    def test_altered_copy_is_refused(self, env, tmp_path):
        app_state, s3 = env
        d = self.local_copy(app_state, tmp_path, content="tampered")
        with pytest.raises(PlanRefusedError, match="sha256"):
            handler(app_state)._pre_apply(definition=d)
        assert stored(s3).status == "pending"

    def test_copy_of_a_taken_plan_is_refused(self, env, tmp_path):
        app_state, s3 = env
        d = self.local_copy(app_state, tmp_path)
        handler(app_state)._move(record(s3), "applying")
        with pytest.raises(PlanRefusedError, match="applying, not pending"):
            handler(app_state)._pre_apply(definition=d)

    def test_legacy_plan_when_required(self, env, tmp_path):
        app_state, s3 = env
        app_state.terraform_options.require_plan_manifest = True
        s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"legacy")
        with pytest.raises(PlanRefusedError, match="no manifest"):
            handler(app_state)._pre_apply(definition=definition(tmp_path))

    def test_unreadable_manifest_is_refused(self, env, tmp_path):
        app_state, s3 = env
        d = self.local_copy(app_state, tmp_path)
        s3.put_object(Bucket=BUCKET, Key=record(s3), Body=b"{}")
        with pytest.raises(PlanRefusedError, match="unreadable"):
            handler(app_state)._pre_apply(definition=d)

    def test_read_error_is_a_refusal(self, env, tmp_path, monkeypatch):
        app_state, _ = env
        h = handler(app_state)

        def fail(key):
            raise BackendError("throttled")

        monkeypatch.setattr(h.store, "current", fail)
        with pytest.raises(PlanRefusedError, match="throttled"):
            h._pre_apply(definition=definition(tmp_path))


class TestFinishing:
    def taken(self, app_state, tmp_path):
        h = handler(app_state)
        d = definition(tmp_path)
        make_plan(Path(d.plan_file))
        h._post_plan(definition=d, result=result())
        h._pre_apply(definition=d)
        return h, d

    def test_failed_apply(self, env, tmp_path):
        app_state, s3 = env
        h, d = self.taken(app_state, tmp_path)
        h._apply_failed(definition=d, result=result(1))
        m = stored(s3)
        assert m.status == "failed"
        assert [c.status for c in m.history] == ["applying", "failed"]
        assert keys(s3, "prefix/run-a/") == [manifest_key(KEY)]

    def test_terraform_never_started_releases_the_plan(self, env, tmp_path):
        app_state, s3 = env
        h, d = self.taken(app_state, tmp_path)
        h._apply_failed(
            definition=d, result=TerraformResult.synthesized(2, "pre hook failed")
        )
        m = stored(s3)
        assert m.status == "pending"
        assert "released" in m.history[-1].reason
        assert KEY in keys(s3, "prefix/run-a/")
        assert handler(app_state).has_plan(definition(tmp_path / "next")) is True

    def test_refusal_error_stage_leaves_the_plan_alone(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        handler(app_state)._apply_failed(definition=definition(tmp_path))
        assert stored(s3).status == "pending"

    def test_teardown_fails_plans_left_applying(self, env, tmp_path):
        app_state, s3 = env
        h, _ = self.taken(app_state, tmp_path)
        h.teardown("dep", str(tmp_path))
        m = stored(s3)
        assert m.status == "failed"
        assert "ended before" in m.history[-1].reason
        h.teardown("dep", str(tmp_path))

    def test_finishing_write_is_retried(self, env, tmp_path):
        app_state, s3 = env
        h, d = self.taken(app_state, tmp_path)
        real = h.store.write
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise BackendError("throttled")
            return real(*args, **kwargs)

        with patch.object(h.store, "write", side_effect=flaky):
            h._post_apply(definition=d)
        assert stored(s3).status == "applied"

    def test_finishing_gives_up(self, env, tmp_path):
        app_state, s3 = env
        h, d = self.taken(app_state, tmp_path)

        def fail(*args, **kwargs):
            raise BackendError("down")

        with patch.object(h.store, "write", side_effect=fail):
            h._finish(d.name, "applied")
        assert stored(s3).status == "applying"

    def test_finishing_a_plan_someone_else_moved(self, env, tmp_path):
        app_state, s3 = env
        h, d = self.taken(app_state, tmp_path)
        handler(app_state)._move(record(s3), "failed", reason="operator")
        h._post_apply(definition=d)
        assert stored(s3).status == "failed"
        assert KEY in keys(s3, "prefix/run-a/")

    def test_new_plan_while_applying_keeps_both_histories(self, env, tmp_path):
        app_state, s3 = env
        h, d = self.taken(app_state, tmp_path)
        applying_key = record(s3)
        save_plan(app_state, tmp_path / "newer", content="v2")
        h._post_apply(definition=d)
        applied = store(s3).read(applying_key)[0]
        assert applied.status == "applied"
        newer = stored(s3)
        assert newer.status == "stale"
        assert KEY not in keys(s3, "prefix/run-a/")


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

        assert stored(s3).status == "applied"
        other = stored(s3, "prefix/run-b/net/terraform.tfplan")
        assert other.status == "stale"
        assert "run-a" in other.history[-1].reason
        assert keys(s3, "prefix/run-b/") == [
            manifest_key("prefix/run-b/net/terraform.tfplan")
        ]
        assert stored(s3, "prefix/run-c/dns/terraform.tfplan").status == "pending"


class TestForcePlan:
    def test_forced_plan_supersedes_the_saved_plan(self, env, tmp_path):
        app_state, s3 = env
        save_plan(app_state, tmp_path)
        app_state.terraform_options.force_plan = True
        handler(app_state)._check_plan("dep", definition(tmp_path))
        m = stored(s3)
        assert m.status == "superseded"
        assert "forced plan" in m.history[-1].reason
        assert keys(s3, "prefix/_tfworker/pending/") == []
        assert keys(s3, "prefix/run-a/") == [manifest_key(KEY)]


class TestPreview:
    @pytest.mark.parametrize("intent", ["preview", "drift"])
    def test_previews_are_stored_apart_and_never_offered(self, env, tmp_path, intent):
        app_state, s3 = env
        app_state.terraform_options.intent = intent
        h = handler(app_state)
        slot = h.get_remote_file("net")
        assert slot == "prefix/_tfworker/previews/run-a/net/terraform.tfplan"
        save_plan(app_state, tmp_path)
        assert stored(s3, slot).storage == "preview"
        assert h.has_plan(definition(tmp_path)) is False
        assert keys(s3, "prefix/_tfworker/pending/") == []

    def test_preview_run_does_not_fetch_saved_plans(self, env, tmp_path):
        app_state, _ = env
        save_plan(app_state, tmp_path)
        app_state.terraform_options.intent = "preview"
        d = definition(tmp_path)
        handler(app_state)._check_plan("dep", d)
        assert not Path(d.plan_file).exists()

    def test_only_the_run_that_made_a_preview_may_apply_it(self, env, tmp_path):
        app_state, s3 = env
        app_state.terraform_options.intent = "preview"
        h = handler(app_state)
        d = definition(tmp_path)
        make_plan(Path(d.plan_file))
        h._post_plan(definition=d, result=result())
        slot = h.get_remote_file("net")
        key = record(s3, slot)

        later = handler(app_state)
        later._plans["net"] = later._slot(slot)
        app_state.root_options.run_id = "run-z"
        with pytest.raises(PlanRefusedError, match="preview plans are never applied"):
            later._pre_apply(definition=d)

        app_state.root_options.run_id = "run-a"
        h._pre_apply(definition=d)
        assert store(s3).read(key)[0].status == "applying"


class TestStages:
    def test_apply_and_destroy_stages(self):
        h = S3Handler()
        apply = h.execution_functions[TerraformAction.APPLY]
        destroy = h.execution_functions[TerraformAction.DESTROY]
        assert apply[TerraformStage.PRE] == h._pre_apply
        assert destroy[TerraformStage.PRE] == h._pre_destroy
        for stages in (apply, destroy):
            assert stages[TerraformStage.POST] == h._post_apply
            assert stages[TerraformStage.ERROR] == h._apply_failed

    def test_store_errors_while_moving_are_logged(self, env, monkeypatch):
        app_state, _ = env
        h = handler(app_state)

        def fail(key):
            raise BackendError("denied")

        monkeypatch.setattr(h.store, "read", fail)
        assert h._move("prefix/x", "stale") is None
