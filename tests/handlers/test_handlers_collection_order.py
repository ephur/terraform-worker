from unittest.mock import MagicMock

import pytest

from tfworker.custom_types import TerraformAction, TerraformStage
from tfworker.exceptions import HandlerError
from tfworker.handlers.base import BaseHandler
from tfworker.handlers.collection import HandlersCollection

ORDER = []


@pytest.fixture(autouse=True)
def clear_order():
    ORDER.clear()
    # reset singleton to ensure fresh handlers for each test
    HandlersCollection._instance = None


class DummyHandler(BaseHandler):
    actions = [TerraformAction.PLAN]

    def __init__(self):
        self._ready = True

    def execute(self, action, stage, deployment, definition, working_dir, result=None):
        ORDER.append(self.tag)


class HandlerA(DummyHandler):
    tag = "a"
    default_priority = {TerraformAction.PLAN: 50}


class HandlerB(DummyHandler):
    tag = "b"
    default_priority = {TerraformAction.PLAN: 60}
    dependencies = {TerraformAction.PLAN: {TerraformStage.POST: ["a"]}}


class HandlerC(DummyHandler):
    tag = "c"
    default_priority = {TerraformAction.PLAN: 40}


class HandlerD(DummyHandler):
    tag = "d"
    default_priority = {TerraformAction.PLAN: 60}
    dependencies = {TerraformAction.PLAN: {TerraformStage.POST: ["missing"]}}


class DummyDef:
    name = "def"


def test_dependency_and_priority_order():
    h = HandlersCollection(
        {
            "a": HandlerA(),
            "b": HandlerB(),
            "c": HandlerC(),
        }
    )
    h.exec_handlers(TerraformAction.PLAN, TerraformStage.POST, "dep", DummyDef(), ".")
    assert ORDER == ["c", "a", "b"]


def test_missing_dependency_ignored():
    h = HandlersCollection(
        {
            "a": HandlerA(),
            "d": HandlerD(),
        }
    )
    h.exec_handlers(TerraformAction.PLAN, TerraformStage.POST, "dep", DummyDef(), ".")
    assert ORDER == ["a", "d"]


class HandlerWithPlan(DummyHandler):
    tag = "with_plan"
    retrieved = False
    plan_usable = True

    def has_plan(self, definition):
        return True

    def get_plan(self, definition):
        self.retrieved = True
        return self.plan_usable


class HandlerWithoutPlan(DummyHandler):
    tag = "without_plan"

    def has_plan(self, definition):
        return False


class TestCheckPlanConflicts:
    def test_no_conflicts_with_no_handlers_having_plans(self):
        h = HandlersCollection(
            {
                "a": HandlerWithoutPlan(),
                "b": HandlerWithoutPlan(),
            }
        )
        # Should not raise
        h.check_plan_conflicts(DummyDef())

    def test_no_conflicts_with_one_handler_having_plan(self):
        h = HandlersCollection(
            {
                "a": HandlerWithPlan(),
                "b": HandlerWithoutPlan(),
            }
        )
        # Should not raise
        h.check_plan_conflicts(DummyDef())

    def test_conflict_with_multiple_handlers_having_plans(self):
        h = HandlersCollection(
            {
                "a": HandlerWithPlan(),
                "b": HandlerWithPlan(),
            }
        )
        with pytest.raises(
            HandlerError, match="Multiple handlers claim to have a plan"
        ):
            h.check_plan_conflicts(DummyDef())

    def test_not_ready_handler_ignored_in_conflict_check(self):
        handler_not_ready = HandlerWithPlan()
        handler_not_ready._ready = False

        h = HandlersCollection(
            {
                "a": HandlerWithPlan(),
                "b": handler_not_ready,
            }
        )
        # Should not raise since handler b is not ready
        h.check_plan_conflicts(DummyDef())


class TestHasAvailablePlan:
    def test_returns_false_when_no_handlers_have_plans(self):
        h = HandlersCollection(
            {
                "a": HandlerWithoutPlan(),
                "b": HandlerWithoutPlan(),
            }
        )
        assert h.has_available_plan(DummyDef()) is False

    def test_returns_true_when_handler_has_plan(self):
        h = HandlersCollection(
            {
                "a": HandlerWithPlan(),
                "b": HandlerWithoutPlan(),
            }
        )
        assert h.has_available_plan(DummyDef()) is True

    def test_raises_on_conflict(self):
        h = HandlersCollection(
            {
                "a": HandlerWithPlan(),
                "b": HandlerWithPlan(),
            }
        )
        with pytest.raises(
            HandlerError, match="Multiple handlers claim to have a plan"
        ):
            h.has_available_plan(DummyDef())

    def test_ignores_not_ready_handlers(self):
        handler_not_ready = HandlerWithPlan()
        handler_not_ready._ready = False

        h = HandlersCollection(
            {
                "a": HandlerWithoutPlan(),
                "b": handler_not_ready,
            }
        )
        assert h.has_available_plan(DummyDef()) is False


class TestGetAvailablePlan:
    def test_retrieves_from_the_handler_with_the_plan(self):
        with_plan = HandlerWithPlan()
        h = HandlersCollection({"a": HandlerWithoutPlan(), "b": with_plan})
        assert h.get_available_plan(DummyDef()) is True
        assert with_plan.retrieved is True

    def test_returns_false_when_no_handler_has_a_plan(self):
        h = HandlersCollection({"a": HandlerWithoutPlan()})
        assert h.get_available_plan(DummyDef()) is False

    def test_returns_false_when_the_plan_is_unusable(self):
        with_plan = HandlerWithPlan()
        with_plan.plan_usable = False
        h = HandlersCollection({"a": with_plan})
        assert h.get_available_plan(DummyDef()) is False

    def test_raises_on_conflict(self):
        h = HandlersCollection({"a": HandlerWithPlan(), "b": HandlerWithPlan()})
        with pytest.raises(
            HandlerError, match="Multiple handlers claim to have a plan"
        ):
            h.get_available_plan(DummyDef())

    def test_ignores_not_ready_handlers(self):
        not_ready = HandlerWithPlan()
        not_ready._ready = False
        h = HandlersCollection({"a": not_ready})
        assert h.get_available_plan(DummyDef()) is False
        assert not_ready.retrieved is False


class TestExecSetupTeardown:
    def test_exec_setup_calls_setup_on_ready_handlers(self):
        calls = []

        class SetupHandler(DummyHandler):
            tag = "s"

            def setup(self, deployment, definitions, working_dir, terraform_options):
                calls.append((deployment, working_dir))

        h = HandlersCollection({"s": SetupHandler()})
        h.exec_setup("prod", MagicMock(), "/tmp", MagicMock())
        assert calls == [("prod", "/tmp")]

    def test_exec_teardown_calls_teardown_on_ready_handlers(self):
        calls = []

        class TeardownHandler(DummyHandler):
            tag = "t"

            def teardown(self, deployment, working_dir):
                calls.append((deployment, working_dir))

        h = HandlersCollection({"t": TeardownHandler()})
        h.exec_teardown("prod", "/tmp")
        assert calls == [("prod", "/tmp")]

    def test_exec_setup_skips_not_ready_handlers(self):
        calls = []

        class NotReadyHandler(DummyHandler):
            tag = "nr"

            def setup(self, deployment, definitions, working_dir, terraform_options):
                calls.append("called")

        handler = NotReadyHandler()
        handler._ready = False
        h = HandlersCollection({"nr": handler})
        h.exec_setup("prod", MagicMock(), "/tmp", MagicMock())
        assert calls == []

    def test_exec_setup_isolates_unexpected_errors(self, mocker):
        """An unexpected setup error is logged and the next handler still runs (#188)."""
        calls = []

        class BrokenHandler(DummyHandler):
            tag = "b"

            def setup(self, deployment, definitions, working_dir, terraform_options):
                raise RuntimeError("boom")

        class OkHandler(DummyHandler):
            tag = "ok"

            def setup(self, deployment, definitions, working_dir, terraform_options):
                calls.append("ok")

        err = mocker.patch("tfworker.util.log.error")
        h = HandlersCollection({"b": BrokenHandler(), "ok": OkHandler()})
        h.exec_setup("prod", MagicMock(), "/tmp", MagicMock())
        assert calls == ["ok"]
        assert "boom" in err.call_args.args[0]

    def test_exec_setup_non_terminating_handler_error_continues(self, mocker):
        calls = []

        class SoftHandler(DummyHandler):
            tag = "soft"

            def setup(self, deployment, definitions, working_dir, terraform_options):
                raise HandlerError("optional", terminate=False)

        class OkHandler(DummyHandler):
            tag = "ok"

            def setup(self, deployment, definitions, working_dir, terraform_options):
                calls.append("ok")

        warn = mocker.patch("tfworker.util.log.warn")
        h = HandlersCollection({"soft": SoftHandler(), "ok": OkHandler()})
        h.exec_setup("prod", MagicMock(), "/tmp", MagicMock())
        assert calls == ["ok"]
        warn.assert_called_once()

    def test_exec_setup_terminating_handler_error_raises(self):
        class FatalHandler(DummyHandler):
            tag = "f"

            def setup(self, deployment, definitions, working_dir, terraform_options):
                raise HandlerError("required", terminate=True)

        h = HandlersCollection({"f": FatalHandler()})
        with pytest.raises(HandlerError, match="required"):
            h.exec_setup("prod", MagicMock(), "/tmp", MagicMock())

    def test_exec_teardown_runs_every_handler_after_a_failure(self, mocker):
        """A failing teardown no longer skips the handlers after it (#188)."""
        calls = []

        class BrokenHandler(DummyHandler):
            tag = "b"

            def teardown(self, deployment, working_dir):
                calls.append("b")
                raise RuntimeError("boom")

        class FatalHandler(DummyHandler):
            tag = "f"

            def teardown(self, deployment, working_dir):
                calls.append("f")
                raise HandlerError("fatal")

        class OkHandler(DummyHandler):
            tag = "ok"

            def teardown(self, deployment, working_dir):
                calls.append("ok")

        mocker.patch("tfworker.util.log.error")
        h = HandlersCollection(
            {"b": BrokenHandler(), "f": FatalHandler(), "ok": OkHandler()}
        )
        with pytest.raises(HandlerError, match="fatal"):
            h.exec_teardown("prod", "/tmp")
        assert calls == ["b", "f", "ok"]

    def test_exec_teardown_unexpected_error_is_logged_not_raised(self, mocker):
        class BrokenHandler(DummyHandler):
            tag = "b"

            def teardown(self, deployment, working_dir):
                raise RuntimeError("boom")

        err = mocker.patch("tfworker.util.log.error")
        h = HandlersCollection({"b": BrokenHandler()})
        h.exec_teardown("prod", "/tmp")
        assert "boom" in err.call_args.args[0]

    def test_exec_teardown_raise_errors_false_only_logs(self, mocker):
        class FatalHandler(DummyHandler):
            tag = "f"

            def teardown(self, deployment, working_dir):
                raise HandlerError("fatal")

        err = mocker.patch("tfworker.util.log.error")
        h = HandlersCollection({"f": FatalHandler()})
        h.exec_teardown("prod", "/tmp", raise_errors=False)
        err.assert_called_once()

    def test_exec_setup_noop_for_base_handler(self):
        h = HandlersCollection({"a": HandlerA()})
        h.exec_setup("prod", MagicMock(), "/tmp", MagicMock())

    def test_exec_teardown_noop_for_base_handler(self):
        h = HandlersCollection({"a": HandlerA()})
        h.exec_teardown("prod", "/tmp")


class SkipRecorder(DummyHandler):
    tag = "skip"

    def __init__(self):
        super().__init__()
        self.skips = []

    def skip(self, action, deployment, definition, reason):
        self.skips.append((action, deployment, definition.name, reason))


class SkipRaiser(DummyHandler):
    tag = "raiser"
    default_priority = {TerraformAction.PLAN: 1}

    def skip(self, action, deployment, definition, reason):
        raise RuntimeError("boom")


class TestExecSkip:
    def test_ready_handlers_for_the_action_hear_the_skip(self):
        recorder = SkipRecorder()
        h = HandlersCollection({"skip": recorder})
        h.exec_skip(TerraformAction.PLAN, "dep", DummyDef(), "plan file exists")
        assert recorder.skips == [
            (TerraformAction.PLAN, "dep", "def", "plan file exists")
        ]

    def test_handlers_for_other_actions_are_not_told(self):
        recorder = SkipRecorder()
        h = HandlersCollection({"skip": recorder})
        h.exec_skip(TerraformAction.APPLY, "dep", DummyDef(), "no saved plan")
        assert recorder.skips == []

    def test_unready_handlers_are_not_told(self):
        recorder = SkipRecorder()
        recorder._ready = False
        h = HandlersCollection({"skip": recorder})
        h.exec_skip(TerraformAction.PLAN, "dep", DummyDef(), "reason")
        assert recorder.skips == []

    def test_a_failing_handler_does_not_stop_the_others(self, mocker):
        error = mocker.patch("tfworker.util.log.error")
        recorder = SkipRecorder()
        h = HandlersCollection({"raiser": SkipRaiser(), "skip": recorder})
        h.exec_skip(TerraformAction.PLAN, "dep", DummyDef(), "reason")
        assert len(recorder.skips) == 1
        assert "raiser" in error.call_args.args[0]

    def test_default_skip_is_a_no_op(self):
        handler = HandlerA()
        assert handler.skip(TerraformAction.PLAN, "dep", DummyDef(), "reason") is None
