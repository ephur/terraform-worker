import pytest

from tfworker.handlers.base import BaseHandler
from tfworker.handlers.collection import HandlersCollection
from tfworker.types import TerraformAction, TerraformStage

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
