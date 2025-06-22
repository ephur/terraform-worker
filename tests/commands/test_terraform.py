from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from tfworker.commands.terraform import (
    TerraformCommand,
    TerraformCommandConfig,
    TerraformResult,
)
from tfworker.definitions.model import Definition
from tfworker.exceptions import HandlerError, HookError, TFWorkerException
from tfworker.types.terraform import TerraformAction, TerraformStage


class DummyAppState:
    class Opts:
        def __init__(self):
            self.stream_output = True
            self.terraform_bin = "/bin/terraform"
            self.b64_encode = False
            self.destroy = False
            self.apply = True
            self.strict_locking = True
            self.target = None
            self.color = False
            self.provider_cache = "/tmp"

    def __init__(self):
        self.terraform_options = self.Opts()
        self.root_options = mock.Mock(log_level="INFO")


class TestTerraformCommandConfig:
    def test_get_params_apply(self):
        cfg = TerraformCommandConfig(DummyAppState())
        params = cfg.get_params(TerraformAction.APPLY, "plan")
        assert "-auto-approve" in params

    def test_has_changes(self):
        r = TerraformResult(2, b"", b"")
        assert r.has_changes() is True

    def test_env_and_debug(self, tmp_path, monkeypatch):
        cmd = make_command(tmp_path)
        cfg = TerraformCommandConfig(cmd._app_state)
        monkeypatch.setenv("FOO", "BAR")
        env = cfg.env
        assert env["AUTH_VAR"] == "1"
        assert env["FOO"] == "BAR"

        cmd._app_state.root_options.log_level = "DEBUG"
        assert cfg.debug is True

    def test_get_params_target(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        cfg = TerraformCommandConfig(cmd._app_state)
        cmd._app_state.terraform_options.target = ["a.b[0]"]
        warn = mocker.patch("tfworker.util.log.warn")
        cfg.get_params(TerraformAction.APPLY, "plan")
        warn.assert_called_once()
        params = cfg.get_params(TerraformAction.PLAN, "plan")
        assert "-target=" in params

    def test_action_property(self, tmp_path):
        cmd = make_command(tmp_path, destroy=True)
        cfg = TerraformCommandConfig(cmd._app_state)
        assert cfg.action == TerraformAction.DESTROY


def make_command(tmp_path, **opts_overrides):
    """Create a TerraformCommand with a minimal AppState for testing."""
    TerraformCommandConfig._instance = None

    class Opts:
        def __init__(self):
            self.stream_output = True
            self.terraform_bin = "/bin/terraform"
            self.b64_encode = False
            self.destroy = False
            self.apply = True
            self.strict_locking = True
            self.target = None
            self.color = False
            self.provider_cache = None
            self.plan = True
            self.limit = None
            for k, v in opts_overrides.items():
                setattr(self, k, v)

    class RootOpts:
        def __init__(self):
            self.log_level = "INFO"
            self.working_dir = str(tmp_path)

    state = SimpleNamespace(
        terraform_options=Opts(),
        root_options=RootOpts(),
        loaded_config=SimpleNamespace(global_vars=SimpleNamespace(template_vars={})),
        authenticators=[SimpleNamespace(env=lambda: {"AUTH_VAR": "1"})],
        providers="providers",
        backend="backend",
        deployment="dep",
        definitions={"def": Definition(name="def", path="module")},
        handlers=mock.Mock(),
        working_dir=Path(tmp_path),
    )

    cmd = TerraformCommand.__new__(TerraformCommand)
    cmd._app_state = state
    cmd._ctx = mock.Mock()
    cmd._ctx.exit = mock.Mock(side_effect=SystemExit)
    return cmd


class TestTerraformCommandMethods:
    def test_prep_providers_sets_cache(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        mprov = mocker.patch("tfworker.util.terraform.mirror_providers")
        cmd.prep_providers()
        assert cmd.app_state.terraform_options.provider_cache
        mprov.assert_called_once()

    def test_prep_providers_existing_cache(self, tmp_path, mocker):
        cmd = make_command(tmp_path, provider_cache=str(tmp_path / "cache"))
        mprov = mocker.patch("tfworker.util.terraform.mirror_providers")
        cmd.prep_providers()
        assert cmd.app_state.terraform_options.provider_cache.endswith("cache")
        mprov.assert_called_once()

    def test_generate_plan_output_json(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        plan = tmp_path / "plan.tfplan"
        plan.write_text("orig")
        cmd.app_state.definitions["def"].plan_file = str(plan)

        mocker.patch(
            "tfworker.commands.terraform.pipe_exec", return_value=(0, b"o", b"e")
        )

        cmd._generate_plan_output_json("def")

        outfile = tmp_path / "plan.tfplan.json"
        assert outfile.read_text() == "oe"

    def test_run_squelch_options(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        defn = cmd.app_state.definitions["def"]
        defn.plan_file = "plan"

        mocker.patch.object(TerraformCommandConfig, "get_params", return_value="params")
        pe = mocker.patch(
            "tfworker.commands.terraform.pipe_exec", return_value=(0, b"", b"")
        )

        defn.squelch_apply_output = True
        cmd._run("def", TerraformAction.APPLY)
        assert pe.call_args.kwargs["stream_output"] is False

        defn.squelch_apply_output = False
        defn.squelch_plan_output = True
        cmd._run("def", TerraformAction.PLAN)
        assert pe.call_args.kwargs["stream_output"] is False

    def test_exec_hook_paths(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        defn = cmd.app_state.definitions["def"]
        mocker.patch(
            "tfworker.commands.terraform.hooks.check_hooks", return_value=False
        )
        hexec = mocker.patch("tfworker.commands.terraform.hooks.hook_exec")
        cmd._exec_hook(defn, TerraformAction.APPLY, TerraformStage.PRE)
        hexec.assert_not_called()

        mocker.patch("tfworker.commands.terraform.hooks.check_hooks", return_value=True)
        cmd._exec_hook(defn, TerraformAction.APPLY, TerraformStage.PRE)
        hexec.assert_called_once()

    def test_exec_hook_error(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        defn = cmd.app_state.definitions["def"]
        mocker.patch("tfworker.commands.terraform.hooks.check_hooks", return_value=True)
        mocker.patch(
            "tfworker.commands.terraform.hooks.hook_exec", side_effect=HookError("boom")
        )
        with pytest.raises(SystemExit):
            cmd._exec_hook(defn, TerraformAction.APPLY, TerraformStage.PRE)
        cmd.ctx.exit.assert_called_with(2)

    def test_exec_terraform_action_flow(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        _ = cmd.app_state.definitions["def"]
        run = mocker.patch.object(
            cmd, "_run", return_value=TerraformResult(0, b"", b"")
        )
        h = cmd.app_state.handlers
        ehook = mocker.patch.object(cmd, "_exec_hook")
        cmd._exec_terraform_action("def", TerraformAction.APPLY)
        run.assert_called_once()
        assert ehook.call_count == 2
        h.exec_handlers.assert_called()

    def test_exec_terraform_action_errors(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        with pytest.raises(TFWorkerException):
            cmd._exec_terraform_action("def", TerraformAction.PLAN)

        cmd.app_state.handlers.exec_handlers.side_effect = HandlerError("boom")
        with pytest.raises(SystemExit):
            cmd._exec_terraform_action("def", TerraformAction.APPLY)
        cmd.ctx.exit.assert_called_with(2)

        cmd.app_state.handlers.exec_handlers.side_effect = None
        cmd.app_state.handlers.exec_handlers.side_effect = [None, HandlerError("bad")]
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(0, b"", b""))
        with pytest.raises(SystemExit):
            cmd._exec_terraform_action("def", TerraformAction.APPLY)
        cmd.ctx.exit.assert_called_with(2)

        cmd.app_state.handlers.exec_handlers.side_effect = None
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(1, b"", b""))
        with pytest.raises(SystemExit):
            cmd._exec_terraform_action("def", TerraformAction.APPLY)
        cmd.ctx.exit.assert_called_with(1)

    def test_exec_terraform_pre_plan(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        ehook = mocker.patch.object(cmd, "_exec_hook")
        cmd._exec_terraform_pre_plan("def")
        ehook.assert_called_once()

        cmd.app_state.handlers.exec_handlers.side_effect = HandlerError("fail")
        with pytest.raises(SystemExit):
            cmd._exec_terraform_pre_plan("def")
        cmd.ctx.exit.assert_called_with(2)

    def test_exec_terraform_plan_branches(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        defn = cmd.app_state.definitions["def"]
        defn.plan_file = tmp_path / "plan.tfplan"

        gen = mocker.patch.object(cmd, "_generate_plan_output_json")
        _ = mocker.patch.object(cmd, "_exec_hook")

        mocker.patch.object(cmd, "_run", return_value=TerraformResult(0, b"", b""))
        unlink = mocker.patch.object(Path, "unlink")
        cmd.app_state.terraform_options.target = ["x"]
        info = mocker.patch("tfworker.util.log.info")
        cmd._exec_terraform_plan("def")
        unlink.assert_called_once()
        info.assert_any_call("targeting resources: x")

        defn.always_apply = True
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(0, b"", b""))
        cmd._exec_terraform_plan("def")
        assert cmd.app_state.definitions["def"].needs_apply

        mocker.patch.object(cmd, "_run", return_value=TerraformResult(1, b"", b""))
        with pytest.raises(SystemExit):
            cmd._exec_terraform_plan("def")
        cmd.ctx.exit.assert_called_with(1)

        mocker.patch.object(cmd, "_run", return_value=TerraformResult(2, b"", b""))
        cmd._exec_terraform_plan("def")
        gen.assert_called()
        assert cmd.app_state.definitions["def"].needs_apply

        cmd.app_state.handlers.exec_handlers.side_effect = HandlerError("h")
        with pytest.raises(SystemExit):
            cmd._exec_terraform_plan("def")
        cmd.ctx.exit.assert_called_with(2)

    def test_terraform_apply_or_destroy(self, tmp_path, mocker):
        cmd = make_command(tmp_path, apply=True)
        cmd.app_state.definitions["def"].needs_apply = True
        run = mocker.patch.object(cmd, "_exec_terraform_action")
        cmd.terraform_apply_or_destroy()
        run.assert_called_once()

        cmd = make_command(tmp_path, destroy=True, limit=["skip"])
        cmd.app_state.definitions["def"].needs_apply = True
        run = mocker.patch.object(cmd, "_exec_terraform_action")
        cmd.terraform_apply_or_destroy()
        run.assert_not_called()

    def test_terraform_init(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        dp = mocker.patch("tfworker.definitions.prepare.DefinitionPrepare")
        run = mocker.patch.object(cmd, "_exec_terraform_action")
        cmd.terraform_init()
        assert dp.called
        assert run.called


class TestTerraformResult:
    def test_logging_and_file(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        TerraformCommandConfig(cmd._app_state)  # initialize singleton
        info = mocker.patch("tfworker.util.log.info")
        res = TerraformResult(0, b"A\n", b"B\n")
        res.log_stdout(TerraformAction.APPLY.value)
        res.log_stderr(TerraformAction.APPLY.value)
        assert info.call_count == 2
        f = tmp_path / "out.txt"
        res.log_file(str(f))
        assert f.read_text() == "A\nB\n"

    def test_typechecking_block(self, mocker):
        import importlib
        import typing

        import tfworker.commands.terraform as t

        mocker.patch.object(typing, "TYPE_CHECKING", True)
        importlib.reload(t)

    def test_properties(self):
        res = TerraformResult(0, b"out", b"err")
        assert res.stdout_str == "out"
        assert res.stderr_str == "out"

    def test_prep_providers_error(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        _ = mocker.patch(
            "tfworker.util.terraform.mirror_providers",
            side_effect=TFWorkerException("fail"),
        )
        with pytest.raises(SystemExit):
            cmd.prep_providers()
        cmd.ctx.exit.assert_called_with(1)

    def test_terraform_init_error(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        prep = mocker.patch("tfworker.definitions.prepare.DefinitionPrepare")
        inst = prep.return_value
        inst.render_templates.side_effect = TFWorkerException("bad")
        with pytest.raises(SystemExit):
            cmd.terraform_init()
        cmd.ctx.exit.assert_called_with(1)

    def test_terraform_plan_paths(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_inst = plan_cls.return_value
        plan_inst.needs_plan.return_value = (True, "reason")
        cmd._exec_terraform_pre_plan = mocker.Mock()
        cmd._exec_terraform_plan = mocker.Mock()
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(0, b"", b""))
        cmd.terraform_plan()
        assert cmd._exec_terraform_plan.call_count == 1

        cmd.app_state.terraform_options.plan = False
        cmd._exec_terraform_plan.reset_mock()
        cmd.terraform_plan()
        cmd._exec_terraform_plan.assert_not_called()

    def test_terraform_plan_existing(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_inst = plan_cls.return_value
        plan_inst.needs_plan.side_effect = [(False, "plan file exists"), (False, "no")]
        cmd.terraform_plan()
        assert cmd.app_state.definitions["def"].needs_apply is True

    def test_terraform_plan_always_apply(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        cmd.app_state.definitions["def"].always_apply = True
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_inst = plan_cls.return_value
        plan_inst.needs_plan.return_value = (True, "reason")
        cmd._exec_terraform_pre_plan = mocker.Mock()
        _ = mocker.patch.object(cmd, "_exec_terraform_plan")
        act = mocker.patch.object(cmd, "_exec_terraform_action")
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(0, b"", b""))
        cmd.terraform_plan()
        act.assert_called_once()

    def test_terraform_apply_or_destroy_skip(self, tmp_path):
        cmd = make_command(tmp_path)
        # no apply or destroy
        cmd.app_state.terraform_options.apply = False
        cmd.app_state.terraform_options.destroy = False
        cmd.terraform_apply_or_destroy()  # should noop
