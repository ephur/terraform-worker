import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
from zipfile import ZipFile

import boto3
import pytest
from moto import mock_aws

from tfworker.backends import Backends
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.exceptions import BackendError, PlanRefusedError
from tfworker.handlers.local_plans import LocalPlanHandler
from tfworker.report import BackendLocation
from tfworker.util.plan_manifest import (
    LocalManifestStore,
    S3ManifestStore,
    manifest_key,
)


def make_plan(path: Path, content="v1") -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr("tfstate", json.dumps({"lineage": "lin", "serial": 2}))
        archive.writestr("content", content)
    return path


def result(code=2):
    r = MagicMock()
    r.exit_code = code
    return r


@pytest.fixture
def app_state(mock_app_state, tmp_path):
    mock_app_state.terraform_options.plan_file_path = str(tmp_path)
    mock_app_state.root_options.backend_plans = False
    mock_app_state.root_options.backend = Backends.S3
    mock_app_state.root_options.run_id = "run-a"
    mock_app_state.backend = MagicMock()
    mock_app_state.backend.location.return_value = BackendLocation(
        backend="s3", state_key="prefix/net/terraform.tfstate"
    )
    return mock_app_state


def handler(app_state) -> LocalPlanHandler:
    h = LocalPlanHandler()
    h._app_state = app_state
    return h


@pytest.fixture
def planned(app_state, tmp_path):
    """A definition whose plan a planning run just saved to disk."""
    d = MagicMock()
    d.name = "net"
    d.plan_file = str(make_plan(tmp_path / "net.tfplan"))
    handler(app_state).execute(
        TerraformAction.PLAN, TerraformStage.POST, "dep", d, str(tmp_path), result()
    )
    return d


def manifest_of(d):
    found = LocalManifestStore().read(manifest_key(d.plan_file))
    return found[0] if found else None


def run(h, action, stage, d, res=None):
    h.execute(action, stage, "dep", d, "/work", res)


class TestReady:
    def test_ready_with_plan_file_path(self, app_state):
        assert handler(app_state).is_ready() is True

    def test_not_ready_with_backend_plans(self, app_state):
        app_state.root_options.backend_plans = True
        assert handler(app_state).is_ready() is False

    def test_not_ready_without_plan_file_path(self, app_state):
        app_state.terraform_options.plan_file_path = None
        assert handler(app_state).is_ready() is False


class TestPlan:
    def test_writes_a_manifest_beside_the_plan(self, planned):
        m = manifest_of(planned)
        assert m.status == "pending"
        assert m.storage == "local"
        assert m.plan_key == planned.plan_file
        assert m.run_id == "run-a"
        assert m.state.state_key == "prefix/net/terraform.tfstate"
        assert m.base_serial == 2

    def test_no_changes_supersedes_an_old_manifest(self, app_state, planned):
        Path(planned.plan_file).unlink()
        run(
            handler(app_state),
            TerraformAction.PLAN,
            TerraformStage.POST,
            planned,
            result(0),
        )
        assert manifest_of(planned).status == "superseded"

    def test_location_unavailable(self, app_state, tmp_path):
        app_state.backend.location.side_effect = BackendError("no")
        d = MagicMock()
        d.name = "net"
        d.plan_file = str(make_plan(tmp_path / "net.tfplan"))
        run(handler(app_state), TerraformAction.PLAN, TerraformStage.POST, d, result())
        assert manifest_of(d).state is None

    def test_write_failure_is_logged(self, app_state, tmp_path, monkeypatch):
        h = handler(app_state)

        def fail(*args, **kwargs):
            raise BackendError("disk full")

        monkeypatch.setattr(h._store, "write", fail)
        d = MagicMock()
        d.name = "net"
        d.plan_file = str(make_plan(tmp_path / "net.tfplan"))
        run(h, TerraformAction.PLAN, TerraformStage.POST, d, result())
        assert manifest_of(d) is None


class TestApplyGate:
    def test_takes_and_applies(self, app_state, planned):
        h = handler(app_state)
        run(h, TerraformAction.APPLY, TerraformStage.PRE, planned)
        assert manifest_of(planned).status == "applying"
        app_state.root_options.backend = None
        run(h, TerraformAction.APPLY, TerraformStage.POST, planned, result(0))
        m = manifest_of(planned)
        assert m.status == "applied"
        assert [c.status for c in m.history] == ["applying", "applied"]

    def test_failed_apply(self, app_state, planned):
        h = handler(app_state)
        run(h, TerraformAction.DESTROY, TerraformStage.PRE, planned)
        run(h, TerraformAction.DESTROY, TerraformStage.ERROR, planned, result(1))
        assert manifest_of(planned).status == "failed"

    def test_teardown_fails_unfinished_applies(self, app_state, planned):
        h = handler(app_state)
        run(h, TerraformAction.APPLY, TerraformStage.PRE, planned)
        h.teardown("dep", "/work")
        assert manifest_of(planned).status == "failed"

    def test_second_apply_is_refused(self, app_state, planned):
        run(handler(app_state), TerraformAction.APPLY, TerraformStage.PRE, planned)
        with pytest.raises(PlanRefusedError, match="applying, not pending"):
            run(handler(app_state), TerraformAction.APPLY, TerraformStage.PRE, planned)

    def test_lost_race_is_refused(self, app_state, planned, monkeypatch):
        h = handler(app_state)
        monkeypatch.setattr(h, "_move", lambda *a, **k: None)
        with pytest.raises(PlanRefusedError, match="no longer pending"):
            run(h, TerraformAction.APPLY, TerraformStage.PRE, planned)

    def test_altered_plan_is_refused(self, app_state, planned):
        make_plan(Path(planned.plan_file), content="other")
        with pytest.raises(PlanRefusedError, match="sha256"):
            run(handler(app_state), TerraformAction.APPLY, TerraformStage.PRE, planned)
        assert manifest_of(planned).status == "pending"

    def test_expired_plan_is_refused_and_marked(self, app_state, planned, monkeypatch):
        h = handler(app_state)
        later = datetime.now(timezone.utc) + timedelta(days=8)
        monkeypatch.setattr(h, "_now", lambda: later)
        with pytest.raises(PlanRefusedError, match="expired"):
            run(h, TerraformAction.APPLY, TerraformStage.PRE, planned)
        assert manifest_of(planned).status == "expired"

    def test_refusal_error_stage_changes_nothing(self, app_state, planned):
        run(handler(app_state), TerraformAction.APPLY, TerraformStage.ERROR, planned)
        assert manifest_of(planned).status == "pending"

    def test_plan_without_manifest_falls_back(self, app_state, tmp_path):
        d = MagicMock()
        d.name = "net"
        d.plan_file = str(make_plan(tmp_path / "net.tfplan"))
        run(handler(app_state), TerraformAction.APPLY, TerraformStage.PRE, d)
        assert manifest_of(d) is None

    def test_plan_without_manifest_when_required(self, app_state, tmp_path):
        app_state.terraform_options.require_plan_manifest = True
        d = MagicMock()
        d.name = "net"
        d.plan_file = str(make_plan(tmp_path / "net.tfplan"))
        with pytest.raises(PlanRefusedError, match="no manifest"):
            run(handler(app_state), TerraformAction.APPLY, TerraformStage.PRE, d)

    def test_unreadable_manifest(self, app_state, planned):
        Path(manifest_key(planned.plan_file)).write_text("{}")
        with pytest.raises(PlanRefusedError, match="unreadable"):
            run(handler(app_state), TerraformAction.APPLY, TerraformStage.PRE, planned)

    def test_manifest_read_error(self, app_state, planned, monkeypatch):
        h = handler(app_state)

        def fail(key):
            raise BackendError("io")

        monkeypatch.setattr(h._store, "read", fail)
        with pytest.raises(PlanRefusedError, match="io"):
            run(h, TerraformAction.APPLY, TerraformStage.PRE, planned)

    def test_move_error_is_logged(self, app_state, planned, monkeypatch):
        h = handler(app_state)

        def fail(*args, **kwargs):
            raise BackendError("io")

        monkeypatch.setattr(h._store, "write", fail)
        assert h._move(manifest_key(planned.plan_file), "stale") is None

    def test_other_stages_do_nothing(self, app_state, planned):
        run(handler(app_state), TerraformAction.INIT, TerraformStage.PRE, planned)
        assert manifest_of(planned).status == "pending"


class TestBackendPlansGoStale:
    def test_local_apply_marks_pending_backend_plans_stale(self, app_state, planned):
        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="test-bucket")
            app_state.authenticators.get.return_value = app_state.authenticators["aws"]
            store = S3ManifestStore(s3, "test-bucket", "prefix")
            key = manifest_key("prefix/net/terraform.tfplan")
            from tfworker.report.plans import PlanManifest

            store.write(
                key,
                PlanManifest(
                    plan_id="backend",
                    run_id="run-b",
                    deployment="dep",
                    definition="net",
                    plan_key="prefix/net/terraform.tfplan",
                    sha256="0" * 64,
                    size=1,
                    created_at=datetime.now(timezone.utc),
                ),
            )
            h = handler(app_state)
            run(h, TerraformAction.APPLY, TerraformStage.PRE, planned)
            run(h, TerraformAction.APPLY, TerraformStage.POST, planned, result(0))
            assert store.read(key)[0].status == "stale"

    def test_no_aws_authenticator(self, app_state):
        app_state.authenticators.get.return_value = None
        assert handler(app_state)._backend_store() is None

    def test_session_failure(self, app_state):
        auth = MagicMock()
        auth.backend_session.client.side_effect = RuntimeError("no creds")
        app_state.authenticators.get.return_value = auth
        assert handler(app_state)._backend_store() is None


def test_app_state_comes_from_the_click_context(mock_click_context):
    from unittest.mock import patch

    with patch("click.get_current_context", return_value=mock_click_context):
        assert LocalPlanHandler().app_state is mock_click_context.obj
