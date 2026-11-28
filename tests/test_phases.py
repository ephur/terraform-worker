from unittest import mock

from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.phases import PhaseDispatcher, PhaseListener


class Recorder(PhaseListener):
    name = "recorder"

    def __init__(self):
        self.calls = []

    def setup(self, deployment):
        self.calls.append(("setup", deployment))

    def phase(
        self, action, stage, definition, working_dir, result=None, lock_wait=None
    ):
        self.calls.append(("phase", action, stage, definition, result, lock_wait))

    def skip(self, action, definition, reason):
        self.calls.append(("skip", action, definition, reason))

    def teardown(self, deployment):
        self.calls.append(("teardown", deployment))


class Broken(PhaseListener):
    name = "broken"

    def _boom(self, *args, **kwargs):
        raise RuntimeError("boom")

    setup = phase = skip = teardown = _boom


def test_base_listener_ignores_everything():
    dispatcher = PhaseDispatcher([PhaseListener()])
    dispatcher.setup("dep")
    dispatcher.phase(TerraformAction.PLAN, TerraformStage.PRE, "d", "/w")
    dispatcher.skip(TerraformAction.PLAN, "d", "why")
    dispatcher.teardown("dep")


def test_every_call_reaches_each_listener_in_order():
    first, second = Recorder(), Recorder()
    dispatcher = PhaseDispatcher([first, second])
    dispatcher.setup("dep")
    dispatcher.phase(
        TerraformAction.APPLY, TerraformStage.POST, "d", "/w", result="r", lock_wait=2.0
    )
    dispatcher.skip(TerraformAction.PLAN, "d", "why")
    dispatcher.teardown("dep")
    expected = [
        ("setup", "dep"),
        ("phase", TerraformAction.APPLY, TerraformStage.POST, "d", "r", 2.0),
        ("skip", TerraformAction.PLAN, "d", "why"),
        ("teardown", "dep"),
    ]
    assert first.calls == expected
    assert second.calls == expected


def test_a_failing_listener_never_stops_the_run_or_the_others(mocker):
    warn = mocker.patch("tfworker.phases.log.warn")
    after = Recorder()
    dispatcher = PhaseDispatcher([Broken(), after])
    dispatcher.setup("dep")
    dispatcher.phase(TerraformAction.PLAN, TerraformStage.PRE, "d", "/w")
    dispatcher.skip(TerraformAction.PLAN, "d", "why")
    dispatcher.teardown("dep")
    assert len(after.calls) == 4
    assert warn.call_args_list[0] == mock.call(
        "broken failed during setup: RuntimeError: boom"
    )
    assert len(warn.call_args_list) == 4
