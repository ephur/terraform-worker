from tfworker.custom_types import TerraformAction, TerraformStage
from tfworker.handlers import BaseHandler, HandlersCollection
from tfworker.handlers.results import BaseHandlerResult


class DummyResult(BaseHandlerResult):
    foo: int


class DummyHandler(BaseHandler):
    actions = [TerraformAction.PLAN]

    def __init__(self):
        self._ready = True

    def execute(self, action, stage, deployment, definition, working_dir, result=None):
        return DummyResult(handler="dummy", action=action, stage=stage, foo=1)


class DummyDef:
    name = "def"


def test_results_store_and_lookup():
    HandlersCollection._instance = None
    hc = HandlersCollection({"dummy": DummyHandler()})
    hc.exec_handlers(TerraformAction.PLAN, TerraformStage.POST, "dep", DummyDef(), ".")
    assert len(hc.results) == 1
    by_name = hc.get_results(handler_name="dummy")
    assert by_name[0].foo == 1
    by_field = hc.find_results("foo", 1)
    assert by_field[0].handler == "dummy"
