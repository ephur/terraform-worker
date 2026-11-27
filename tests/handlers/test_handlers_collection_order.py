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

    def test_exec_setup_propagates_errors(self):
        class BrokenHandler(DummyHandler):
            tag = "b"

            def setup(self, deployment, definitions, working_dir, terraform_options):
                raise RuntimeError("boom")

        h = HandlersCollection({"b": BrokenHandler()})
        with pytest.raises(RuntimeError, match="boom"):
            h.exec_setup("prod", MagicMock(), "/tmp", MagicMock())

    def test_exec_teardown_propagates_errors(self):
        class BrokenHandler(DummyHandler):
            tag = "b"

            def teardown(self, deployment, working_dir):
                raise RuntimeError("boom")

        h = HandlersCollection({"b": BrokenHandler()})
        with pytest.raises(RuntimeError, match="boom"):
            h.exec_teardown("prod", "/tmp")

    def test_exec_setup_noop_for_base_handler(self):
        h = HandlersCollection({"a": HandlerA()})
        h.exec_setup("prod", MagicMock(), "/tmp", MagicMock())

    def test_exec_teardown_noop_for_base_handler(self):
        h = HandlersCollection({"a": HandlerA()})
        h.exec_teardown("prod", "/tmp")
