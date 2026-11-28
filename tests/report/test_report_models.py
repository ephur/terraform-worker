import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tfworker.report import (
    BackendLocation,
    ChangeSummary,
    DefinitionResult,
    DefinitionStatus,
    PhaseResult,
    Provenance,
    RunRecord,
    new_ulid,
)
from tfworker.report.schema import file_name, json_schemas, main, write_schemas
from tfworker.report.status import merge_status

T0 = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
LOC = BackendLocation(
    backend="s3",
    account_id="123456789012",
    region="us-east-1",
    bucket="b",
    state_key="p/def1/terraform.tfstate",
)


class TestUlid:
    def test_shape_and_order(self):
        a = new_ulid(1_000, b"\x00" * 10)
        b = new_ulid(1_001, b"\x00" * 10)
        assert len(a) == 26
        assert a < b
        assert new_ulid(0, b"\x00" * 10) == "0" * 26
        assert len(new_ulid()) == 26

    @pytest.mark.parametrize(
        "ts,entropy", [(-1, b"\x00" * 10), (2**48, b"\x00" * 10), (1, b"\x00")]
    )
    def test_invalid(self, ts, entropy):
        with pytest.raises(ValueError):
            new_ulid(ts, entropy)


class TestChangeSummary:
    def test_from_plan_json(self):
        plan = {
            "resource_changes": [
                {
                    "address": "a.new",
                    "change": {"actions": ["create"], "after": {"secret": "x"}},
                },
                {"address": "a.mod", "change": {"actions": ["update"]}},
                {"address": "a.gone", "change": {"actions": ["delete"]}},
                {"address": "a.swap", "change": {"actions": ["delete", "create"]}},
                {"address": "a.same", "change": {"actions": ["no-op"]}},
                {"address": "data.x", "change": {"actions": ["read"]}},
                {
                    "address": "a.imp",
                    "change": {"actions": ["no-op"], "importing": {"id": "i-1"}},
                },
            ]
        }
        summary = ChangeSummary.from_plan_json(plan)
        assert (summary.add, summary.change, summary.destroy, summary.import_) == (
            2,
            1,
            2,
            1,
        )
        assert summary.total == 6
        assert [r.address for r in summary.resources] == [
            "a.new",
            "a.mod",
            "a.gone",
            "a.swap",
            "a.imp",
        ]
        dumped = summary.model_dump_json(by_alias=True)
        assert "secret" not in dumped
        assert '"import":1' in dumped
        assert summary.counts_only().resources == []

    def test_empty_plan(self):
        assert ChangeSummary.from_plan_json({}).total == 0

    def test_location_identity(self):
        assert LOC.identity == "s3/123456789012/us-east-1/b/p/def1/terraform.tfstate"
        bare = BackendLocation(backend="s3", state_key="k")
        assert bare.identity == "s3////k"


def _run(run_id="r1", at=T0, intent=None, storage="backend", **phases):
    result = DefinitionResult(
        definition="def1",
        state=LOC,
        changes=phases.pop("changes", None),
        base_lineage="lin",
        base_serial=4,
        applied_serial=phases.pop("applied_serial", None),
    )
    offset = 0
    for action, status in phases.items():
        offset += 60
        result.phases[action] = PhaseResult(
            action=action,
            status=status,
            started_at=at + timedelta(seconds=offset - 30),
            finished_at=at + timedelta(seconds=offset),
            duration_seconds=30,
            reason="boom" if status == "failed" else None,
        )
    run = RunRecord(
        record_id=f"rec-{run_id}",
        run_id=run_id,
        deployment="dep",
        started_at=at,
        finished_at=at + timedelta(minutes=10),
        status="failed" if result.failed else "succeeded",
        provenance=Provenance(intent=intent, plan_storage=storage, labels={"o": "ci"}),
        definitions=[result],
    )
    return run, result


CHANGES = ChangeSummary(add=1)


class TestMergeStatus:
    def test_plan_with_changes_is_pending(self):
        run, result = _run(plan="succeeded", changes=CHANGES)
        status = merge_status(None, run, result, T0)
        assert status.last_plan.run_id == "r1"
        assert status.last_plan.changes.add == 1
        assert status.last_plan.base_serial == 4
        assert status.last_plan.labels == {"o": "ci"}
        assert status.pending_plan.run_id == "r1"
        assert status.last_apply is None
        assert status.last_failure is None

    @pytest.mark.parametrize(
        "intent,storage", [("preview", "backend"), (None, "local"), (None, "run")]
    )
    def test_not_pending(self, intent, storage):
        run, result = _run(
            intent=intent, storage=storage, plan="succeeded", changes=CHANGES
        )
        assert merge_status(None, run, result, T0).pending_plan is None

    def test_no_changes_not_pending(self):
        run, result = _run(plan="succeeded", changes=ChangeSummary())
        assert merge_status(None, run, result, T0).pending_plan is None

    def test_apply_clears_pending_and_drift(self):
        run, result = _run(plan="succeeded", changes=CHANGES)
        status = merge_status(None, run, result, T0)
        status.drift = status.pending_plan
        run2, result2 = _run(
            "r2", T0 + timedelta(hours=1), apply="succeeded", applied_serial=5
        )
        status = merge_status(status, run2, result2, T0 + timedelta(hours=1))
        assert status.pending_plan is None
        assert status.drift is None
        assert status.last_apply.applied_serial == 5
        assert status.last_apply.changes is None
        assert status.last_plan.run_id == "r1"
        assert status.updated_by_run == "r2"

    def test_plan_and_apply_in_one_run(self):
        run, result = _run(plan="succeeded", apply="succeeded", changes=CHANGES)
        status = merge_status(None, run, result, T0)
        assert status.pending_plan is None
        assert status.last_apply.status == "succeeded"

    def test_phases_fold_in_run_order_whatever_the_map_order(self):
        run, result = _run(plan="succeeded", apply="succeeded", changes=CHANGES)
        result.phases = dict(reversed(list(result.phases.items())))
        assert list(result.phases) == ["apply", "plan"]
        status = merge_status(None, run, result, T0)
        assert status.pending_plan is None
        assert status.last_apply.status == "succeeded"

    def test_failure(self):
        run, result = _run(plan="succeeded", apply="failed", changes=CHANGES)
        status = merge_status(None, run, result, T0)
        assert status.last_failure.action == "apply"
        assert status.last_failure.reason == "boom"
        assert status.last_apply.status == "failed"
        assert status.pending_plan.run_id == "r1"

    def test_failed_plan_is_not_pending(self):
        run, result = _run(plan="failed")
        status = merge_status(None, run, result, T0)
        assert status.pending_plan is None
        assert status.last_plan.status == "failed"

    def test_skipped_phases_are_ignored(self):
        run, result = _run(plan="skipped")
        status = merge_status(None, run, result, T0)
        assert status.last_plan is None

    def test_drift(self):
        run, result = _run(intent="drift", plan="succeeded", changes=CHANGES)
        status = merge_status(None, run, result, T0)
        assert status.drift.run_id == "r1"
        assert status.pending_plan is None
        run2, result2 = _run(
            "r2",
            T0 + timedelta(hours=1),
            intent="drift",
            plan="succeeded",
            changes=ChangeSummary(),
        )
        assert merge_status(status, run2, result2, T0).drift is None

    def test_late_plan_does_not_return_after_apply(self):
        """A run that planned before another run applied finishes last."""
        applied, applied_result = _run(
            "b",
            T0 + timedelta(hours=1),
            plan="succeeded",
            apply="succeeded",
            changes=CHANGES,
        )
        status = merge_status(None, applied, applied_result, T0)
        late, late_result = _run(
            "a", T0, intent="drift", plan="succeeded", changes=CHANGES
        )
        status = merge_status(status, late, late_result, T0 + timedelta(hours=2))
        assert status.pending_plan is None
        assert status.drift is None
        late, late_result = _run("a2", T0, plan="succeeded", changes=CHANGES)
        assert merge_status(status, late, late_result, T0).pending_plan is None

    def test_no_change_plan_clears_older_pending(self):
        run, result = _run(plan="succeeded", changes=CHANGES)
        status = merge_status(None, run, result, T0)
        later, later_result = _run(
            "r2",
            T0 + timedelta(hours=1),
            intent="preview",
            storage="local",
            plan="succeeded",
            changes=ChangeSummary(),
        )
        assert merge_status(status, later, later_result, T0).pending_plan is None

    def test_unknown_changes_leave_pending_and_drift(self):
        run, result = _run(intent="drift", plan="succeeded", changes=CHANGES)
        status = merge_status(None, run, result, T0)
        later, later_result = _run(
            "r2",
            T0 + timedelta(hours=1),
            intent="drift",
            plan="succeeded",
            changes=None,
        )
        status = merge_status(status, later, later_result, T0)
        assert status.drift.run_id == "r1"
        assert status.last_plan.run_id == "r2"

    def test_unknown_fields_survive(self):
        run, result = _run(plan="succeeded", changes=CHANGES)
        data = json.loads(
            merge_status(None, run, result, T0).model_dump_json(by_alias=True)
        )
        data["future_field"] = {"x": 1}
        status = DefinitionStatus.model_validate(data)
        later, later_result = _run("r2", T0 + timedelta(hours=1), plan="succeeded")
        merged = merge_status(status, later, later_result, T0)
        assert json.loads(merged.model_dump_json(by_alias=True))["future_field"] == {
            "x": 1
        }

    def test_older_run_does_not_move_status_back(self):
        newer, newer_result = _run(
            "new", T0 + timedelta(hours=1), plan="succeeded", changes=CHANGES
        )
        status = merge_status(None, newer, newer_result, T0)
        older, older_result = _run(
            "old", T0, plan="succeeded", apply="succeeded", changes=CHANGES
        )
        status = merge_status(status, older, older_result, T0)
        assert status.last_plan.run_id == "new"
        assert status.pending_plan.run_id == "new"
        assert status.last_apply.run_id == "old"

    def test_naive_times_become_utc(self):
        run, result = _run(plan="succeeded")
        status = merge_status(None, run, result, datetime(2026, 11, 27, 12))
        assert status.updated_at.tzinfo is not None
        assert status.last_plan.at.tzinfo is not None
        phase = PhaseResult(
            action="plan", status="succeeded", started_at=datetime(2026, 1, 1)
        )
        assert phase.started_at.tzinfo is not None
        assert (
            RunRecord(
                record_id="x",
                run_id="x",
                deployment="d",
                started_at=datetime(2026, 1, 1),
                status="succeeded",
            ).started_at.tzinfo
            is not None
        )

    def test_round_trip(self):
        run, result = _run(plan="succeeded", changes=CHANGES)
        status = merge_status(None, run, result, T0)
        text = status.model_dump_json(by_alias=True)
        assert json.loads(text)["schema"] == "tfworker.status/v1"
        assert DefinitionStatus.model_validate_json(text) == status
        record = json.loads(run.model_dump_json(by_alias=True))
        assert record["schema"] == "tfworker.run/v1"
        assert RunRecord.model_validate(record) == run


class TestSchema:
    def test_ids(self):
        schemas = json_schemas()
        assert set(schemas) == {"tfworker.run/v1", "tfworker.status/v1"}
        assert schemas["tfworker.run/v1"]["$id"] == "tfworker.run/v1"
        assert file_name("tfworker.run/v1") == "run-v1.schema.json"

    def test_write(self, tmp_path):
        write_schemas(tmp_path / "out")
        main([str(tmp_path / "cli")])
        for directory in ("out", "cli"):
            assert json.loads((tmp_path / directory / "run-v1.schema.json").read_text())

    def test_published_schemas_are_current(self):
        published = Path(__file__).parents[2] / "docs" / "schemas"
        for schema_id, schema in json_schemas().items():
            on_disk = json.loads((published / file_name(schema_id)).read_text())
            assert (
                on_disk == schema
            ), f"run python -m tfworker.report.schema {published}"
