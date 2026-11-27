"""`worker plans list|discard|gc` against moto."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import boto3
import botocore.exceptions
import pytest
from click.testing import CliRunner
from moto import mock_aws

from tfworker.backends import Backends
from tfworker.cli import cli
from tfworker.commands.base import BaseCommand
from tfworker.commands.plans import PlansCommand
from tfworker.exceptions import BackendError
from tfworker.report import BackendLocation, ChangeSummary, DefinitionStatus
from tfworker.report.models import RunEvent
from tfworker.report.plans import PlanManifest
from tfworker.util.plan_manifest import S3ManifestStore

BUCKET = "plans-bucket"
PREFIX = "prefix"
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def state_key(name):
    return f"{PREFIX}/{name}/terraform.tfstate"


@pytest.fixture
def env():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        put_state(s3, "def", "lin", 4)
        put_state(s3, "other", "lin2", 1)
        backend = mock.Mock()
        backend.read_report.return_value = None
        state = SimpleNamespace(
            root_options=SimpleNamespace(
                backend=Backends.S3,
                backend_bucket=BUCKET,
                backend_prefix=PREFIX,
                run_id="gc-run",
            ),
            authenticators={"aws": SimpleNamespace(backend_session=boto3.Session())},
            definitions={"def": object(), "other": object()},
            backend=backend,
        )
        yield state, s3


def put_state(s3, name, lineage, serial):
    return s3.put_object(
        Bucket=BUCKET,
        Key=state_key(name),
        Body=json.dumps({"lineage": lineage, "serial": serial}).encode(),
    )


def command(state):
    def init(self, deployment=None, **kwargs):
        self._app_state = state
        self._ctx = mock.Mock()
        self._ctx.exit = mock.Mock(side_effect=SystemExit)

    with mock.patch.object(BaseCommand, "__init__", init):
        cmd = PlansCommand("dep")
    cmd._now = lambda: NOW
    return cmd


def save(s3, plan_id, definition="def", status="pending", **fields):
    """A saved backend plan: its record, slot pointer, plan object and index entry."""
    store = S3ManifestStore(s3, BUCKET, PREFIX)
    plan_key = f"{PREFIX}/{definition}/terraform.tfplan"
    values = dict(
        plan_id=plan_id,
        run_id="planner",
        deployment="dep",
        definition=definition,
        plan_key=plan_key,
        base_lineage="lin" if definition == "def" else "lin2",
        base_serial=4 if definition == "def" else 1,
        sha256="0" * 64,
        size=4,
        created_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
    )
    values.update(fields)
    manifest = PlanManifest(**values)
    if status != "pending":
        at = datetime.now(timezone.utc) - timedelta(minutes=30)
        manifest = manifest.moved(status, at=at, run_id="r1")
    key = store.record_key(definition, plan_id)
    store.write(key, manifest, create_only=True)
    store.point(plan_key, key)
    s3.put_object(Bucket=BUCKET, Key=plan_key, Body=b"plan")
    return key


def status_of(s3, key):
    return S3ManifestStore(s3, BUCKET, PREFIX).read(key)[0]


def exists(s3, key):
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:
        return False


class TestSetup:
    def test_needs_the_s3_backend(self, env):
        state, _ = env
        state.root_options.backend = None
        with pytest.raises(SystemExit):
            command(state)

    def test_needs_aws_credentials(self, env):
        state, _ = env
        state.authenticators = {}
        with pytest.raises(SystemExit):
            command(state)


class TestList:
    def test_lists_oldest_first(self, env, capsys):
        state, s3 = env
        save(s3, "B", created_at=NOW - timedelta(minutes=5))
        save(
            s3,
            "A",
            definition="other",
            destroy=True,
            expires_at=None,
            changes=ChangeSummary(add=1, change=2, destroy=3),
        )
        command(state).list_plans((), None, False)
        lines = capsys.readouterr().out.splitlines()
        assert lines[0].startswith("A  other  pending destroy  +1 ~2 -3")
        assert "expires never" in lines[0]
        assert lines[1].startswith("B  def  pending  -")

    def test_filters(self, env, capsys):
        state, s3 = env
        save(s3, "A")
        save(s3, "B", status="applying")
        save(s3, "C", definition="other")
        command(state).list_plans(("applying",), None, False)
        assert [x.split()[0] for x in capsys.readouterr().out.splitlines()] == ["B"]
        command(state).list_plans((), "other", False)
        assert [x.split()[0] for x in capsys.readouterr().out.splitlines()] == ["C"]

    def test_json_is_manifests(self, env, capsys):
        state, s3 = env
        save(s3, "A")
        command(state).list_plans((), None, True)
        plans = json.loads(capsys.readouterr().out)
        assert PlanManifest.model_validate(plans[0]).plan_id == "A"
        assert plans[0]["schema"] == "tfworker.plan/v1"

    def test_empty(self, env, capsys):
        state, _ = env
        command(state).list_plans((), None, False)
        assert capsys.readouterr().out == "no saved plans\n"

    def test_unreadable_record_is_skipped(self, env, capsys):
        state, s3 = env
        save(s3, "A")
        s3.put_object(
            Bucket=BUCKET, Key=f"{PREFIX}/_tfworker/plans/def/X.json", Body=b"{"
        )
        command(state).list_plans((), None, False)
        assert capsys.readouterr().out.startswith("A  def")

    def test_record_read_error_is_skipped(self, env, capsys):
        state, s3 = env
        save(s3, "A")
        cmd = command(state)
        with mock.patch.object(cmd.store, "read", side_effect=BackendError("boom")):
            cmd.list_plans((), None, False)
        assert "no saved plans" in capsys.readouterr().out

    def test_listing_error_exits(self, env):
        state, _ = env
        cmd = command(state)
        with mock.patch.object(cmd.store, "records", side_effect=BackendError("no")):
            with pytest.raises(SystemExit):
                cmd.list_plans((), None, False)


class TestDiscard:
    def test_discards_a_pending_plan(self, env, capsys):
        state, s3 = env
        key = save(s3, "A")
        command(state).discard(["A"], "bad change", None)
        manifest = status_of(s3, key)
        assert manifest.status == "discarded"
        assert manifest.history[-1].reason == "discarded: bad change"
        assert manifest.history[-1].run_id == "gc-run"
        assert not exists(s3, f"{PREFIX}/def/terraform.tfplan")
        assert not exists(s3, f"{PREFIX}/_tfworker/pending/def/A")
        assert "discarded A (def)" in capsys.readouterr().out

    def test_unknown_plan(self, env):
        state, s3 = env
        key = save(s3, "A")
        with pytest.raises(SystemExit):
            command(state).discard(["Z", "A"], "why", None)
        assert status_of(s3, key).status == "discarded"

    def test_only_pending_plans(self, env):
        state, s3 = env
        key = save(s3, "A", status="applying")
        with pytest.raises(SystemExit):
            command(state).discard(["A"], "why", "def")
        assert status_of(s3, key).status == "applying"
        assert exists(s3, f"{PREFIX}/def/terraform.tfplan")

    def test_backend_error(self, env):
        state, s3 = env
        save(s3, "A")
        cmd = command(state)
        with mock.patch.object(cmd.store, "read", side_effect=BackendError("boom")):
            with pytest.raises(SystemExit):
                cmd.discard(["A"], "why", None)


class TestGc:
    def gc(self, state, dry_run=False, timeout=None, as_json=False):
        command(state).gc(dry_run, timeout, as_json)

    def test_fresh_plan_is_kept(self, env, capsys):
        state, s3 = env
        key = save(s3, "A")
        self.gc(state)
        assert status_of(s3, key).status == "pending"
        assert "no saved plans to retire" in capsys.readouterr().out

    def test_unchanged_version_skips_the_state_download(self, env):
        state, s3 = env
        s3.put_bucket_versioning(
            Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
        )
        version = put_state(s3, "def", "lin", 4)["VersionId"]
        key = save(s3, "A", base_state_version_id=version)
        cmd = command(state)
        with mock.patch.object(cmd, "_state_base") as base:
            cmd.gc(False, None, False)
        base.assert_not_called()
        assert status_of(s3, key).status == "pending"

    def test_stale_outside_a_recorded_run(self, env, capsys):
        state, s3 = env
        key = save(s3, "A", base_state_version_id="old")
        put_state(s3, "def", "lin", 5)
        self.gc(state)
        manifest = status_of(s3, key)
        assert manifest.status == "stale"
        assert manifest.history[-1].reason == "gc: state changed outside a recorded run"
        assert not exists(s3, f"{PREFIX}/def/terraform.tfplan")
        assert "retired A (def) as stale" in capsys.readouterr().out

    def test_stale_by_a_recorded_run(self, env):
        state, s3 = env
        key = save(s3, "A")
        put_state(s3, "def", "lin", 5)
        status = DefinitionStatus(
            deployment="dep",
            definition="def",
            state=BackendLocation(backend="s3", state_key=state_key("def")),
            updated_at=NOW,
            updated_by_run="r9",
            last_apply=RunEvent(
                run_id="r9", action="apply", status="succeeded", at=NOW
            ),
        )
        state.backend.read_report.return_value = (
            status.model_dump_json(by_alias=True),
            "tag",
        )
        self.gc(state)
        assert status_of(s3, key).history[-1].reason == "gc: state changed by run r9"
        state.backend.read_report.assert_called_with("status/def.json")

    def test_unreadable_run_status(self, env):
        state, s3 = env
        key = save(s3, "A")
        put_state(s3, "def", "lin", 5)
        state.backend.read_report.side_effect = BackendError("no")
        self.gc(state)
        assert "outside a recorded run" in status_of(s3, key).history[-1].reason

    def test_expired(self, env):
        state, s3 = env
        key = save(s3, "A", expires_at=NOW - timedelta(minutes=1))
        self.gc(state)
        assert status_of(s3, key).status == "expired"

    def test_orphaned_by_config(self, env):
        state, s3 = env
        key = save(s3, "A", definition="gone")
        self.gc(state)
        manifest = status_of(s3, key)
        assert manifest.status == "discarded"
        assert "no longer in the configuration" in manifest.history[-1].reason

    def test_orphaned_by_state(self, env):
        state, s3 = env
        key = save(s3, "A")
        s3.delete_object(Bucket=BUCKET, Key=state_key("def"))
        self.gc(state)
        manifest = status_of(s3, key)
        assert manifest.status == "discarded"
        assert manifest.history[-1].reason == "gc: orphaned: its state no longer exists"

    def test_plan_for_new_state_is_kept(self, env):
        state, s3 = env
        s3.delete_object(Bucket=BUCKET, Key=state_key("def"))
        key = save(s3, "A", base_lineage=None, base_serial=0)
        self.gc(state)
        assert status_of(s3, key).status == "pending"

    def test_state_location_from_the_manifest(self, env):
        state, s3 = env
        s3.put_object(
            Bucket=BUCKET,
            Key="elsewhere/def.tfstate",
            Body=json.dumps({"lineage": "lin", "serial": 9}).encode(),
        )
        location = BackendLocation(backend="s3", state_key="elsewhere/def.tfstate")
        key = save(s3, "A", state=location)
        self.gc(state)
        assert status_of(s3, key).status == "stale"

    def test_unreadable_state_is_skipped(self, env):
        state, s3 = env
        key = save(s3, "A")
        s3.put_object(Bucket=BUCKET, Key=state_key("def"), Body=b"not json")
        self.gc(state)
        assert status_of(s3, key).status == "pending"

    def test_state_check_error_is_skipped(self, env):
        state, s3 = env
        key = save(s3, "A")
        cmd = command(state)
        error = botocore.exceptions.ClientError(
            {"Error": {"Code": "AccessDenied"}}, "HeadObject"
        )
        with mock.patch.object(cmd._client, "head_object", side_effect=error):
            cmd.gc(False, None, False)
        assert status_of(s3, key).status == "pending"

    def test_dry_run_changes_nothing(self, env, capsys):
        state, s3 = env
        key = save(s3, "A", expires_at=NOW - timedelta(minutes=1))
        gone = save(s3, "B", definition="other")
        S3ManifestStore(s3, BUCKET, PREFIX).write(
            gone, status_of(s3, gone).moved("stale"), if_match=None
        )
        s3.put_object(
            Bucket=BUCKET, Key=f"{PREFIX}/_tfworker/pending/other/B", Body=gone.encode()
        )
        self.gc(state, dry_run=True)
        assert status_of(s3, key).status == "pending"
        assert exists(s3, f"{PREFIX}/def/terraform.tfplan")
        assert exists(s3, f"{PREFIX}/_tfworker/pending/other/B")
        assert "would retire A (def) as expired" in capsys.readouterr().out

    def test_json(self, env, capsys):
        state, s3 = env
        save(s3, "A", expires_at=NOW - timedelta(minutes=1))
        self.gc(state, as_json=True)
        swept = json.loads(capsys.readouterr().out)
        assert swept == [
            {
                "plan_id": "A",
                "definition": "def",
                "plan_key": f"{PREFIX}/def/terraform.tfplan",
                "record_key": f"{PREFIX}/_tfworker/plans/def/A.json",
                "check": "expired",
                "status": "expired",
                "reason": swept[0]["reason"],
                "done": True,
            }
        ]

    def test_plan_that_changed_first_is_left_alone(self, env, capsys):
        state, s3 = env
        key = save(s3, "A", expires_at=NOW - timedelta(minutes=1))
        cmd = command(state)
        with mock.patch("tfworker.commands.plans.move", return_value=None):
            cmd.gc(False, None, False)
        assert "it changed first" in capsys.readouterr().out
        assert status_of(s3, key).status == "pending"

    def test_retire_error_is_logged(self, env):
        state, s3 = env
        save(s3, "A", expires_at=NOW - timedelta(minutes=1))
        cmd = command(state)
        with (
            mock.patch(
                "tfworker.commands.plans.move", side_effect=BackendError("boom")
            ),
            mock.patch("tfworker.commands.plans.log.warn") as warn,
        ):
            cmd.gc(False, None, False)
        assert "boom" in warn.call_args[0][0]

    def test_index_listing_error_exits(self, env):
        state, _ = env
        cmd = command(state)
        with mock.patch.object(cmd.store, "keys_for", side_effect=BackendError("x")):
            with pytest.raises(SystemExit):
                cmd.gc(False, None, False)


class TestStuckApplying:
    def test_old_applying_plan_is_failed(self, env, capsys):
        state, s3 = env
        key = save(s3, "A", status="applying")
        cmd = command(state)
        cmd._now = lambda: datetime.now(timezone.utc) + timedelta(hours=7)
        cmd.gc(False, 6 * 3600, False)
        manifest = status_of(s3, key)
        assert manifest.status == "failed"
        assert "applying since" in manifest.history[-1].reason
        assert "by run r1" in manifest.history[-1].reason

    def test_recent_applying_plan_is_kept(self, env):
        state, s3 = env
        key = save(s3, "A", status="applying")
        cmd = command(state)
        cmd._now = lambda: datetime.now(timezone.utc)
        cmd.gc(False, 6 * 3600, False)
        assert status_of(s3, key).status == "applying"

    def test_old_record_taken_recently_is_kept(self, env):
        state, s3 = env
        key = save(s3, "A", status="applying")
        cmd = command(state)
        cmd._now = lambda: datetime.now(timezone.utc)
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with mock.patch.object(cmd.store, "records", return_value=[(key, old)]):
            cmd.gc(False, 3600, False)
        assert status_of(s3, key).status == "applying"

    def test_without_a_timeout_applying_plans_are_left(self, env):
        state, s3 = env
        key = save(s3, "A", status="applying")
        cmd = command(state)
        cmd._now = lambda: datetime.now(timezone.utc) + timedelta(days=7)
        cmd.gc(False, None, False)
        assert status_of(s3, key).status == "applying"


class TestCli:
    @pytest.fixture
    def run(self, tmp_path):
        config = tmp_path / "worker.yaml"
        config.write_text("terraform:\n  definitions: {}\n")

        def invoke(*args):
            with (
                mock.patch("tfworker.cli.RootCommand"),
                mock.patch("tfworker.cli.PlansCommand") as cmd,
            ):
                result = CliRunner().invoke(
                    cli, ["--config-file", str(config), "plans", *args]
                )
            return result, cmd

        return invoke

    def test_list(self, run):
        result, cmd = run("list", "dep", "--status", "pending", "-d", "def", "--json")
        assert result.exit_code == 0, result.output
        cmd.return_value.list_plans.assert_called_once_with(("pending",), "def", True)

    def test_discard(self, run):
        result, cmd = run("discard", "dep", "A", "B", "--reason", "why")
        assert result.exit_code == 0, result.output
        cmd.return_value.discard.assert_called_once_with(("A", "B"), "why", None)

    def test_discard_needs_a_reason(self, run):
        result, _ = run("discard", "dep", "A")
        assert result.exit_code == 2

    def test_gc(self, run):
        result, cmd = run("gc", "dep", "--dry-run", "--applying-timeout", "6h")
        assert result.exit_code == 0, result.output
        cmd.return_value.gc.assert_called_once_with(True, 21600.0, False)

    def test_gc_rejects_a_bad_timeout(self, run):
        result, _ = run("gc", "dep", "--applying-timeout", "soon")
        assert result.exit_code == 2
        assert "soon" in result.output


def test_now_is_aware():
    assert PlansCommand._now().tzinfo is timezone.utc
