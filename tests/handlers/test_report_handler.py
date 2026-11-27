import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from moto import mock_aws

from tfworker.backends.s3 import S3Backend
from tfworker.commands.terraform import TerraformResult
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.definitions.model import Definition
from tfworker.exceptions import BackendError, ReportConflictError
from tfworker.handlers.report import ReportConfig, ReportHandler
from tfworker.report import DefinitionStatus, RunRecord

T0 = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
INIT, PLAN, APPLY, DESTROY = (
    TerraformAction.INIT,
    TerraformAction.PLAN,
    TerraformAction.APPLY,
    TerraformAction.DESTROY,
)
PRE, POST, ERROR = TerraformStage.PRE, TerraformStage.POST, TerraformStage.ERROR

PLAN_JSON = {
    "resource_changes": [
        {
            "address": "aws_s3_bucket.b",
            "change": {"actions": ["create"], "after": {"x": "secret"}},
        }
    ]
}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


@pytest.fixture
def backend(mock_authenticators):
    with mock_aws():
        yield S3Backend(mock_authenticators, "test-deployment")


def make_handler(backend, tmp_path, run_id=None, plan=True, apply=False, **opts):
    handler = ReportHandler(ReportConfig(**opts))
    handler._app_state = SimpleNamespace(
        backend=backend,
        deployment="test-deployment",
        root_options=SimpleNamespace(
            run_id=run_id,
            run_label=["origin=ci"],
            backend_plans=True,
            config_file=[str(tmp_path / "worker.yaml")],
        ),
        terraform_options=SimpleNamespace(
            plan=plan, apply=apply, destroy=False, intent=None, plan_file_path=None
        ),
        terraform_version=(1, 13, 0),
    )
    handler._now = Clock()
    handler.setup("test-deployment", None, str(tmp_path), None)
    return handler


def make_definition(tmp_path, name="def1"):
    definition = Definition(name=name, path=name)
    definition.plan_file = tmp_path / f"{name}.tfplan"
    return definition


def result(code, stderr=b""):
    return TerraformResult(
        code, b"", stderr, started_at=T0, finished_at=T0 + timedelta(seconds=42)
    )


def run_phase(handler, action, definition, tmp_path, code=0, stderr=b""):
    handler.execute(action, PRE, "test-deployment", definition, str(tmp_path))
    stage = ERROR if code == 1 else POST
    handler.execute(
        action,
        stage,
        "test-deployment",
        definition,
        str(tmp_path),
        result(code, stderr),
    )


def plan_with_changes(handler, definition, tmp_path):
    definition.needs_apply = True
    definition.plan_file.with_suffix(".tfplan.json").write_text(json.dumps(PLAN_JSON))
    run_phase(handler, PLAN, definition, tmp_path, code=2)


def read(backend, path):
    found = backend.read_report(path)
    return json.loads(found[0]) if found else None


def run_record(backend, run_id):
    keys = backend._s3_client.list_objects_v2(
        Bucket="test-bucket", Prefix="prefix/_tfworker/runs/"
    ).get("Contents", [])
    paths = [k["Key"] for k in keys if k["Key"].endswith(f"/{run_id}.json")]
    assert len(paths) == 1, paths
    return RunRecord.model_validate(read(backend, paths[0].split("_tfworker/", 1)[1]))


def status(backend, name="def1"):
    return DefinitionStatus.model_validate(read(backend, f"status/{name}.json"))


def put_state(backend, serial):
    backend._s3_client.put_object(
        Bucket="test-bucket",
        Key="prefix/def1/terraform.tfstate",
        Body=json.dumps({"lineage": "lin-1", "serial": serial, "resources": []}),
    )


class TestRuns:
    def test_plan_only_run(self, backend, tmp_path):
        put_state(backend, 3)
        handler = make_handler(backend, tmp_path, run_id="run-1")
        definition = make_definition(tmp_path)
        definition.lock_wait_seconds = 12.5
        run_phase(handler, INIT, definition, tmp_path)
        plan_with_changes(handler, definition, tmp_path)
        handler.teardown("test-deployment", str(tmp_path))

        record = run_record(backend, "run-1")
        assert record.status == "succeeded"
        assert record.actions == ["plan"]
        assert record.run_id_generated is False
        assert record.provenance.labels == {"origin": "ci"}
        assert record.provenance.plan_storage == "backend"
        assert record.provenance.terraform_version == "1.13.0"
        (result_,) = record.definitions
        assert result_.state.account_id == "123456789012"
        assert result_.base_lineage == "lin-1"
        assert result_.base_serial == 3
        assert result_.phases["init"].lock_wait_seconds == 12.5
        assert result_.phases["plan"].duration_seconds == 42
        assert result_.phases["plan"].exit_code == 2
        assert result_.changes.add == 1
        assert result_.changes.resources[0].address == "aws_s3_bucket.b"

        current = status(backend)
        assert current.pending_plan.run_id == "run-1"
        assert current.last_plan.changes.resources == []
        raw = backend.read_report("status/def1.json")[0] + json.dumps(
            read(backend, "runs/2026/11/27/run-1.json") or {}
        )
        assert "secret" not in raw

    def test_plan_and_apply_run(self, backend, tmp_path):
        put_state(backend, 3)
        handler = make_handler(backend, tmp_path, run_id="run-2", apply=True)
        definition = make_definition(tmp_path)
        (tmp_path / "definitions" / "def1" / ".terraform" / "modules").mkdir(
            parents=True
        )
        (
            tmp_path
            / "definitions"
            / "def1"
            / ".terraform"
            / "modules"
            / "modules.json"
        ).write_text(
            json.dumps(
                {
                    "Modules": [
                        {
                            "Key": "m",
                            "Source": "/terraform-modules/m",
                            "Dir": "/terraform-modules/m",
                        }
                    ]
                }
            )
        )
        run_phase(handler, INIT, definition, tmp_path)
        plan_with_changes(handler, definition, tmp_path)
        put_state(backend, 4)
        run_phase(handler, APPLY, definition, tmp_path)
        handler.teardown("test-deployment", str(tmp_path))

        (result_,) = run_record(backend, "run-2").definitions
        assert result_.applied_serial == 4
        assert result_.module_sources[0].kind == "local"
        assert result_.tags == ["local-module-paths"]
        current = status(backend)
        assert current.pending_plan is None
        assert current.last_apply.applied_serial == 4
        assert current.last_apply.tags == ["local-module-paths"]

    def test_failed_run(self, backend, tmp_path):
        handler = make_handler(backend, tmp_path, run_id="run-3", apply=True)
        definition = make_definition(tmp_path)
        plan_with_changes(handler, definition, tmp_path)
        run_phase(
            handler,
            APPLY,
            definition,
            tmp_path,
            code=1,
            stderr=b"\nsome noise\nError: AccessDenied\n  more\n",
        )
        handler.teardown("test-deployment", str(tmp_path))

        record = run_record(backend, "run-3")
        assert record.status == "failed"
        apply = record.definitions[0].phases["apply"]
        assert apply.status == "failed"
        assert apply.reason == "Error: AccessDenied"
        current = status(backend)
        assert current.last_failure.action == "apply"
        assert current.pending_plan.run_id == "run-3"

    def test_skipped_definition(self, backend, tmp_path):
        handler = make_handler(backend, tmp_path, run_id="run-4")
        ran, skipped = make_definition(tmp_path), make_definition(tmp_path, "def2")
        run_phase(handler, PLAN, ran, tmp_path)
        handler.skip(PLAN, "test-deployment", skipped, "no changes since last plan")
        handler.skip(PLAN, "test-deployment", ran, "ignored, already ran")
        handler.teardown("test-deployment", str(tmp_path))

        record = run_record(backend, "run-4")
        phases = {r.definition: r.phases["plan"] for r in record.definitions}
        assert phases["def1"].status == "succeeded"
        assert phases["def2"].status == "skipped"
        assert phases["def2"].reason == "no changes since last plan"
        assert status(backend, "def2").last_plan is None
        assert status(backend).pending_plan is None

    def test_generated_run_id(self, backend, tmp_path):
        handler = make_handler(backend, tmp_path)
        run_phase(handler, PLAN, make_definition(tmp_path), tmp_path)
        handler.teardown("test-deployment", str(tmp_path))
        assert len(handler.run_id) == 26
        assert run_record(backend, handler.run_id).run_id_generated is True

    def test_nothing_ran(self, backend, tmp_path):
        handler = make_handler(backend, tmp_path, run_id="run-5")
        handler.teardown("test-deployment", str(tmp_path))
        assert backend.read_report("status/def1.json") is None

    def test_resources_off(self, backend, tmp_path):
        handler = make_handler(
            backend, tmp_path, run_id="run-6", resources=False, state_serials=False
        )
        definition = make_definition(tmp_path)
        plan_with_changes(handler, definition, tmp_path)
        handler.teardown("test-deployment", str(tmp_path))
        (result_,) = run_record(backend, "run-6").definitions
        assert result_.changes.add == 1
        assert result_.changes.resources == []
        assert result_.base_serial is None

    def test_record_is_never_replaced(self, backend, tmp_path):
        for _ in range(2):
            handler = make_handler(backend, tmp_path, run_id="same")
            handler._now = lambda: T0
            run_phase(handler, PLAN, make_definition(tmp_path), tmp_path)
            handler.teardown("test-deployment", str(tmp_path))
        assert run_record(backend, "same").run_id == "same"


class TestConcurrency:
    def test_concurrent_status_updates_keep_both(self, backend, tmp_path):
        """A writer that loses the conditional write re-reads and merges."""
        first = make_handler(backend, tmp_path, run_id="plan-run")
        second = make_handler(
            backend, tmp_path, run_id="apply-run", plan=False, apply=True
        )
        definition = make_definition(tmp_path)
        plan_with_changes(first, definition, tmp_path)
        run_phase(
            second,
            APPLY,
            make_definition(tmp_path),
            tmp_path,
            code=1,
            stderr=b"Error: x",
        )

        real_read = backend.read_report
        barrier = threading.Barrier(2)

        def read_then_wait(path):
            found = real_read(path)
            if path.startswith(
                "status/"
            ) and threading.current_thread().name.startswith("w"):
                try:
                    barrier.wait(timeout=2)
                except threading.BrokenBarrierError:
                    pass
            return found

        backend.read_report = read_then_wait
        threads = [
            threading.Thread(
                target=h.teardown, args=("test-deployment", str(tmp_path)), name=f"w{i}"
            )
            for i, h in enumerate((first, second))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        backend.read_report = real_read
        current = status(backend)
        assert current.last_plan.run_id == "plan-run"
        assert current.last_failure.run_id == "apply-run"

    def test_gives_up_after_retries(self, backend, tmp_path, mocker):
        handler = make_handler(backend, tmp_path, run_id="r", status_retries=2)
        run_phase(handler, PLAN, make_definition(tmp_path), tmp_path)
        write = mocker.patch.object(backend, "write_report")
        write.side_effect = [None, ReportConflictError("x"), ReportConflictError("x")]
        handler.teardown("test-deployment", str(tmp_path))
        assert write.call_count == 3


class TestErrorsNeverBreakRuns:
    def test_backend_errors_are_logged(self, backend, tmp_path, mocker):
        handler = make_handler(backend, tmp_path, run_id="r")
        mocker.patch.object(
            backend, "state_version", side_effect=BackendError("denied")
        )
        definition = make_definition(tmp_path)
        run_phase(handler, PLAN, definition, tmp_path)
        mocker.patch.object(backend, "write_report", side_effect=BackendError("denied"))
        mocker.patch.object(backend, "read_report", side_effect=BackendError("denied"))
        handler.teardown("test-deployment", str(tmp_path))

    def test_unexpected_errors_are_logged(self, backend, tmp_path, mocker):
        handler = make_handler(backend, tmp_path, run_id="r")
        mocker.patch.object(backend, "location", side_effect=RuntimeError("boom"))
        run_phase(handler, PLAN, make_definition(tmp_path), tmp_path)

    def test_unreadable_status_is_replaced(self, backend, tmp_path):
        backend.write_report("status/def1.json", "not json")
        handler = make_handler(backend, tmp_path, run_id="r")
        run_phase(handler, PLAN, make_definition(tmp_path), tmp_path)
        handler.teardown("test-deployment", str(tmp_path))
        assert status(backend).last_plan.run_id == "r"

    def test_unreadable_plan_json(self, backend, tmp_path):
        handler = make_handler(backend, tmp_path, run_id="r")
        definition = make_definition(tmp_path)
        definition.needs_apply = True
        run_phase(handler, PLAN, definition, tmp_path, code=2)
        handler.teardown("test-deployment", str(tmp_path))
        assert run_record(backend, "r").definitions[0].changes.total == 0


class TestReadiness:
    def test_ready_with_report_storage(self, backend, tmp_path):
        handler = make_handler(backend, tmp_path)
        assert handler.is_ready()
        handler._app_state.backend = SimpleNamespace()
        assert not handler.is_ready()

    def test_app_state_from_click(self, mocker):
        ctx = mocker.MagicMock()
        mocker.patch("click.get_current_context", return_value=ctx)
        assert ReportHandler(ReportConfig()).app_state is ctx.obj

    def test_provenance_variants(self, backend, tmp_path, mocker):
        handler = make_handler(backend, tmp_path)
        root = handler._app_state.root_options
        root.backend_plans = False
        root.config_file = str(tmp_path / "worker.yaml")
        handler._app_state.terraform_options.plan_file_path = "/plans"
        handler._app_state.terraform_version = "1.9.0"
        provenance = handler._provenance()
        assert provenance.plan_storage == "local"
        assert provenance.terraform_version == "1.9.0"
        handler._app_state.terraform_options.plan_file_path = None
        root.config_file = []
        assert handler._provenance().plan_storage == "run"
        mocker.patch(
            "tfworker.handlers.report.metadata.version",
            side_effect=__import__("importlib").metadata.PackageNotFoundError,
        )
        mocker.patch("getpass.getuser", side_effect=KeyError("uid"))
        provenance = handler._provenance()
        assert provenance.worker_version is None
        assert provenance.user is None

    def test_failure_reason_without_stderr(self):
        assert ReportHandler._failure_reason(None) is None
        assert ReportHandler._failure_reason(SimpleNamespace(stderr=b"\n \n")) is None
        assert (
            ReportHandler._failure_reason(SimpleNamespace(stderr=b"plain\n")) == "plain"
        )

    def test_phase_without_result(self, backend, tmp_path):
        handler = make_handler(backend, tmp_path, run_id="r")
        definition = make_definition(tmp_path)
        handler.execute(DESTROY, PRE, "d", definition, str(tmp_path))
        handler.execute(DESTROY, ERROR, "d", definition, str(tmp_path), None)
        phase = handler._results["def1"].phases["destroy"]
        assert phase.status == "failed"
        assert phase.duration_seconds == 1
