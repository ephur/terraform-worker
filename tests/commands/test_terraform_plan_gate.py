"""The saved-plan apply gate driven through TerraformCommand, against moto."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zipfile import ZipFile

import boto3
import pytest
from moto import mock_aws

from tests.commands.test_terraform import make_command
from tfworker.backends import Backends
from tfworker.commands.terraform import TerraformResult
from tfworker.custom_types.terraform import TerraformAction
from tfworker.definitions.plan import DefinitionPlan
from tfworker.handlers.collection import HandlersCollection
from tfworker.handlers.s3 import S3Handler
from tfworker.util.plan_manifest import S3ManifestStore

BUCKET = "gate-bucket"
SLOT = "prefix/def/terraform.tfplan"


def make_plan(path: Path, content="v1", serial=4) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        archive.writestr("tfstate", json.dumps({"lineage": "lin", "serial": serial}))
        archive.writestr("content", content)
    return path


@pytest.fixture
def gate(tmp_path):
    """A command whose handlers are a real s3 handler on a moto bucket."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        s3.put_object(
            Bucket=BUCKET,
            Key="prefix/def/terraform.tfstate",
            Body=json.dumps({"lineage": "lin", "serial": 4}).encode(),
        )
        cmd = make_command(tmp_path / "work", plan=False, backend_plans=True)
        state = cmd.app_state
        state.root_options.backend = Backends.S3
        state.root_options.backend_bucket = BUCKET
        state.root_options.backend_prefix = "prefix"
        state.authenticators = {"aws": SimpleNamespace(backend_session=boto3.Session())}
        options = state.terraform_options
        options.force_plan = False
        options.intent = None
        options.plan_ttl = "7d"
        options.require_plan_manifest = False
        options.always_apply = False
        state.backend = mock.Mock()
        state.backend.location.side_effect = RuntimeError("not needed")

        HandlersCollection._instance = None
        handler = S3Handler()
        handler._app_state = state
        state.handlers = HandlersCollection({"s3": handler})
        with mock.patch(
            "tfworker.handlers.collection.click.get_current_context",
            return_value=SimpleNamespace(obj=state),
        ):
            yield cmd, s3, tmp_path
        HandlersCollection._instance = None


def save_plan(cmd, tmp_path, content="v1"):
    """A planning run's upload, with its own handler."""
    planner = S3Handler()
    planner._app_state = cmd.app_state
    d = SimpleNamespace(name="def", plan_file=str(tmp_path / "planner" / "def.tfplan"))
    make_plan(Path(d.plan_file), content)
    result = mock.Mock(exit_code=2, outside_terraform=False)
    result.log_file = lambda p: Path(p).write_text("log")
    planner._post_plan(definition=d, result=result)
    return d


def status(s3):
    store = S3ManifestStore(s3, BUCKET, "prefix")
    return store.read(store.current(SLOT))[0].status


def fetch(cmd):
    cmd._fetch_saved_plans(DefinitionPlan(cmd.ctx, cmd.app_state))
    return cmd.app_state.definitions["def"]


def apply(cmd, mocker, hook=True, code=0):
    mocker.patch.object(cmd, "_exec_hook", return_value=hook)
    run = mocker.patch.object(cmd, "_run", return_value=TerraformResult(code, b"", b""))
    cmd._exec_terraform_action("def", TerraformAction.APPLY, exit_on_error=False)
    return run


class TestApplyOnlyRun:
    def test_saved_plan_is_fetched_taken_and_applied(self, gate, mocker):
        cmd, s3, tmp_path = gate
        save_plan(cmd, tmp_path)
        assert fetch(cmd).needs_apply is True
        run = apply(cmd, mocker)
        run.assert_called_once()
        assert status(s3) == "applied"

    def test_failed_apply_is_recorded_on_the_plan(self, gate, mocker):
        cmd, s3, tmp_path = gate
        save_plan(cmd, tmp_path)
        fetch(cmd)
        apply(cmd, mocker, code=1)
        assert status(s3) == "failed"
        assert cmd.app_state.definitions["def"].apply_failed is True

    def test_refused_plan_is_an_apply_failure(self, gate, mocker):
        cmd, s3, tmp_path = gate
        save_plan(cmd, tmp_path)
        definition = fetch(cmd)
        make_plan(Path(definition.plan_file), "swapped")
        run = apply(cmd, mocker)
        run.assert_not_called()
        assert "sha256" in cmd.apply_errors["def"]
        assert cmd.app_state.definitions["def"].apply_failed is True
        assert status(s3) == "pending"

    def test_failed_pre_hook_releases_the_plan(self, gate, mocker):
        cmd, s3, tmp_path = gate
        save_plan(cmd, tmp_path)
        fetch(cmd)
        run = apply(cmd, mocker, hook=False)
        run.assert_not_called()
        assert status(s3) == "pending"

    def test_plan_already_taken_is_not_fetched(self, gate, mocker):
        cmd, s3, tmp_path = gate
        save_plan(cmd, tmp_path)
        store = S3ManifestStore(s3, BUCKET, "prefix")
        key = store.current(SLOT)
        manifest, tag = store.read(key)
        store.write(key, manifest.moved("applying", run_id="other"), if_match=tag)
        assert fetch(cmd).needs_apply is False

    def test_destroy_plan_is_not_applied_by_an_apply_run(self, gate, mocker):
        cmd, s3, tmp_path = gate
        cmd.app_state.terraform_options.plan_destroy = True
        save_plan(cmd, tmp_path)
        cmd.app_state.terraform_options.plan_destroy = False
        fetch(cmd)
        run = apply(cmd, mocker)
        run.assert_not_called()
        assert "destroy plan" in cmd.apply_errors["def"]
        assert status(s3) == "pending"


class TestPlanFileWithBackendPlans:
    def test_copy_on_disk_is_gated(self, gate, mocker):
        cmd, s3, tmp_path = gate
        planner = save_plan(cmd, tmp_path)
        cmd.app_state.terraform_options.plan_file_path = str(tmp_path / "plans")
        definition = cmd.app_state.definitions["def"]
        DefinitionPlan(cmd.ctx, cmd.app_state).set_plan_file(definition)
        Path(definition.plan_file).write_bytes(Path(planner.plan_file).read_bytes())
        assert fetch(cmd).needs_apply is True
        apply(cmd, mocker)
        assert status(s3) == "applied"

    def test_altered_copy_on_disk_is_refused(self, gate, mocker):
        cmd, s3, tmp_path = gate
        save_plan(cmd, tmp_path)
        cmd.app_state.terraform_options.plan_file_path = str(tmp_path / "plans")
        definition = cmd.app_state.definitions["def"]
        DefinitionPlan(cmd.ctx, cmd.app_state).set_plan_file(definition)
        make_plan(Path(definition.plan_file), "tampered")
        fetch(cmd)
        run = apply(cmd, mocker)
        run.assert_not_called()
        assert status(s3) == "pending"
