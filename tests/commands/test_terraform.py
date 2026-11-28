from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import click
import pytest

import tfworker.util.log as log
from tfworker.commands.terraform import (
    TerraformCommand,
    TerraformCommandConfig,
    TerraformResult,
)
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.definitions.model import Definition
from tfworker.exceptions import (
    HandlerError,
    HookError,
    LockUnavailableError,
    TFWorkerException,
)


class DummyAppState:
    class Opts:
        def __init__(self):
            self.stream_output = True
            self.terraform_bin = "/bin/terraform"
            self.b64_encode = False
            self.destroy = False
            self.apply = True
            self.plan = False
            self.plan_destroy = False
            self.strict_locking = True
            self.target = None
            self.color = False
            self.provider_cache = "/tmp"

    def __init__(self):
        self.terraform_options = self.Opts()
        self.root_options = mock.Mock(log_level="INFO")


class TestTerraformEnv:
    def test_terraform_env_without_authenticators(self, monkeypatch):
        """A missing authenticators collection is treated as empty."""
        from tfworker.commands.terraform import terraform_env

        monkeypatch.setenv("FROM_OS", "1")
        env = terraform_env(SimpleNamespace(authenticators=None))
        assert env["FROM_OS"] == "1"
        assert env["TF_IN_AUTOMATION"] == "1"


class TestTerraformCommandConfig:
    def test_get_params_apply(self):
        cfg = TerraformCommandConfig(DummyAppState())
        params = cfg.get_params(TerraformAction.APPLY, "plan")
        assert "-auto-approve" in params
        assert "plan" in params

    def test_get_params_destroy(self):
        state = DummyAppState()
        state.terraform_options.destroy = True
        cfg = TerraformCommandConfig(state)
        params = cfg.get_params(TerraformAction.DESTROY, "plan.tfplan")
        assert "-auto-approve" in params
        assert "plan.tfplan" in params

    def test_get_params_destroy_plan(self):
        state = DummyAppState()
        state.terraform_options.destroy = True
        cfg = TerraformCommandConfig(state)
        params = cfg.get_params(TerraformAction.PLAN, "plan.tfplan")
        assert "-destroy" in params
        assert "plan.tfplan" in params

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

    def test_env_is_not_cached(self, tmp_path):
        """Credentials can expire, so each command resolves the env again."""
        cmd = make_command(tmp_path)
        calls = []

        def auth_env():
            calls.append(len(calls))
            return {"AWS_SESSION_TOKEN": f"token-{len(calls)}"}

        cmd._app_state.authenticators = [SimpleNamespace(env=auth_env)]
        cfg = TerraformCommandConfig(cmd._app_state)

        first = cfg.env["AWS_SESSION_TOKEN"]
        second = cfg.env["AWS_SESSION_TOKEN"]

        assert (first, second) == ("token-1", "token-2")

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

    # root options are their own model in the real app state; keep them off Opts
    # so a method reading the wrong one fails here as it would in production
    root_overrides = {
        key: opts_overrides.pop(key)
        for key in ("backend_plans", "run_id", "working_dir")
        if key in opts_overrides
    }

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
            self.plan = False
            self.plan_destroy = False
            self.plan_file_path = None
            self.limit = None
            self.plan_failures = True
            self.fail_on_plan_error = True
            self.init_failures = True
            self.fail_on_init_error = True
            self.apply_failures = True
            self.fail_on_apply_error = True
            for k, v in opts_overrides.items():
                setattr(self, k, v)

    class RootOpts:
        def __init__(self):
            self.log_level = "INFO"
            self.working_dir = str(tmp_path)
            self.backend_plans = False
            self.run_id = None
            for k, v in root_overrides.items():
                setattr(self, k, v)

    state = SimpleNamespace(
        terraform_options=Opts(),
        root_options=RootOpts(),
        loaded_config=SimpleNamespace(
            global_vars=SimpleNamespace(template_vars={}),
            parallel_options=SimpleNamespace(
                max_preparation_workers=8, max_init_workers=4
            ),
        ),
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

    def test_prep_providers_uses_resolved_working_dir(self, tmp_path, mocker):
        """Without --working-dir, mirror into app_state.working_dir (#181)."""
        cmd = make_command(tmp_path, working_dir=None)
        mprov = mocker.patch("tfworker.util.terraform.mirror_providers")
        cmd.prep_providers()
        assert mprov.call_args.args[2] == str(tmp_path)

    def test_run_uses_resolved_working_dir(self, tmp_path, mocker):
        """Without --working-dir, terraform must run in the real temp dir (#181)."""
        cmd = make_command(tmp_path, working_dir=None)
        cmd.app_state.definitions["def"].plan_file = "plan"
        mocker.patch.object(TerraformCommandConfig, "get_params", return_value="")
        pe = mocker.patch("tfworker.util.system.pipe_exec", return_value=(0, b"", b""))
        cmd._run("def", TerraformAction.APPLY)
        cwd = Path(pe.call_args.kwargs["cwd"])
        assert "None" not in cwd.parts
        assert cwd == (tmp_path / "definitions" / "def").resolve()

    def test_generate_plan_output_json_uses_resolved_working_dir(
        self, tmp_path, mocker
    ):
        cmd = make_command(tmp_path, working_dir=None)
        plan = tmp_path / "plan.tfplan"
        plan.write_text("orig")
        cmd.app_state.definitions["def"].plan_file = str(plan)
        pe = mocker.patch(
            "tfworker.commands.terraform.pipe_exec", return_value=(0, b"o", b"e")
        )
        cmd._generate_plan_output_json("def")
        cwd = Path(pe.call_args.kwargs["cwd"])
        assert cwd == (tmp_path / "definitions" / "def").resolve()

    def test_generate_plan_output_json(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        plan = tmp_path / "plan.tfplan"
        plan.write_text("orig")
        cmd.app_state.definitions["def"].plan_file = str(plan)

        # this path calls pipe_exec directly, not through pipe_exec_logged
        mocker.patch(
            "tfworker.commands.terraform.pipe_exec", return_value=(0, b"o", b"e")
        )

        cmd._generate_plan_output_json("def")

        outfile = tmp_path / "plan.tfplan.json"
        assert outfile.read_text() == "oe"

    def test_run_logs_structured_context(self, tmp_path, mocker):
        """The start record carries the definition/action; the argv is debug."""
        cmd = make_command(tmp_path)
        cmd.app_state.definitions["def"].plan_file = "plan"
        mocker.patch.object(TerraformCommandConfig, "get_params", return_value="params")
        mocker.patch("tfworker.util.system.pipe_exec", return_value=(0, b"", b""))
        info = mocker.patch("tfworker.util.log.info")
        debug = mocker.patch("tfworker.util.log.debug")

        cmd._run("def", TerraformAction.APPLY)

        info.assert_called_once_with(
            {
                "message": "running terraform apply for def",
                "definition": "def",
                "terraform_action": "apply",
            }
        )
        argv_records = [
            c.args[0]
            for c in debug.call_args_list
            if isinstance(c.args[0], dict) and "running cmd" in c.args[0]["message"]
        ]
        assert len(argv_records) == 1
        assert argv_records[0]["definition"] == "def"
        assert argv_records[0]["terraform_action"] == "apply"
        assert argv_records[0]["command"] == "terraform apply"
        assert "/bin/terraform apply params" in argv_records[0]["message"]

    def test_run_squelch_options(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        defn = cmd.app_state.definitions["def"]
        defn.plan_file = "plan"

        mocker.patch.object(TerraformCommandConfig, "get_params", return_value="params")
        pe = mocker.patch("tfworker.util.system.pipe_exec", return_value=(0, b"", b""))

        defn.squelch_apply_output = True
        cmd._run("def", TerraformAction.APPLY)
        assert pe.call_args.kwargs["stream_output"] is False

        defn.squelch_apply_output = False
        defn.squelch_plan_output = True
        cmd._run("def", TerraformAction.PLAN)
        assert pe.call_args.kwargs["stream_output"] is False

    def test_run_stream_logging_context(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        defn = cmd.app_state.definitions["def"]
        defn.plan_file = "plan"

        mocker.patch.object(TerraformCommandConfig, "get_params", return_value="params")
        pe = mocker.patch("tfworker.util.system.pipe_exec", return_value=(0, b"", b""))

        cmd._run("def", TerraformAction.APPLY)

        assert pe.call_args.kwargs["stream_output"] is True
        assert pe.call_args.kwargs["stream_log_level"] == log.LogLevel.INFO
        assert "stream_log_context" not in pe.call_args.kwargs

    def test_run_json_aggregates_output(self, tmp_path, mocker):
        old_format = log.log_format
        log.log_format = log.LogFormat.JSON
        cmd = make_command(tmp_path)
        defn = cmd.app_state.definitions["def"]
        defn.plan_file = "plan"

        mocker.patch.object(TerraformCommandConfig, "get_params", return_value="params")
        pe = mocker.patch(
            "tfworker.util.system.pipe_exec",
            return_value=(0, b"stdout", b"stderr"),
        )
        aggregate = mocker.patch(
            "tfworker.commands.terraform.log.log_subprocess_result"
        )

        cmd._run("def", TerraformAction.APPLY)

        assert pe.call_args.kwargs["stream_output"] is False
        aggregate.assert_called_once_with(
            command="terraform apply",
            exit_code=0,
            stdout=b"stdout",
            stderr=b"stderr",
            level=log.LogLevel.INFO,
            extra={
                "definition": "def",
                "terraform_action": "apply",
            },
            message="terraform apply output for def",
        )
        log.log_format = old_format

    def test_run_plan_exit_code_2_logs_as_info(self, tmp_path, mocker):
        """Test that terraform plan exit code 2 (changes detected) logs as INFO, not ERROR"""
        old_format = log.log_format
        log.log_format = log.LogFormat.JSON
        cmd = make_command(tmp_path)
        defn = cmd.app_state.definitions["def"]
        defn.plan_file = "plan"

        mocker.patch.object(TerraformCommandConfig, "get_params", return_value="params")
        mocker.patch(
            "tfworker.util.system.pipe_exec",
            return_value=(2, b"stdout", b"stderr"),
        )
        aggregate = mocker.patch(
            "tfworker.commands.terraform.log.log_subprocess_result"
        )

        cmd._run("def", TerraformAction.PLAN)

        aggregate.assert_called_once_with(
            command="terraform plan",
            exit_code=2,
            stdout=b"stdout",
            stderr=b"stderr",
            level=log.LogLevel.INFO,  # Exit code 2 for plan should be INFO
            extra={
                "definition": "def",
                "terraform_action": "plan",
            },
            message="terraform plan output for def",
        )
        log.log_format = old_format

    def test_run_plan_exit_code_1_logs_as_error(self, tmp_path, mocker):
        """Test that terraform plan exit code 1 (actual error) logs as ERROR"""
        old_format = log.log_format
        log.log_format = log.LogFormat.JSON
        cmd = make_command(tmp_path)
        defn = cmd.app_state.definitions["def"]
        defn.plan_file = "plan"

        mocker.patch.object(TerraformCommandConfig, "get_params", return_value="params")
        mocker.patch(
            "tfworker.util.system.pipe_exec",
            return_value=(1, b"stdout", b"stderr"),
        )
        aggregate = mocker.patch(
            "tfworker.commands.terraform.log.log_subprocess_result"
        )

        cmd._run("def", TerraformAction.PLAN)

        aggregate.assert_called_once_with(
            command="terraform plan",
            exit_code=1,
            stdout=b"stdout",
            stderr=b"stderr",
            level=log.LogLevel.ERROR,  # Exit code 1 should always be ERROR
            extra={
                "definition": "def",
                "terraform_action": "plan",
            },
            message="terraform plan output for def",
        )
        log.log_format = old_format

    def test_run_apply_exit_code_2_logs_as_error(self, tmp_path, mocker):
        """Test that non-plan commands with exit code 2 still log as ERROR"""
        old_format = log.log_format
        log.log_format = log.LogFormat.JSON
        cmd = make_command(tmp_path)
        defn = cmd.app_state.definitions["def"]
        defn.plan_file = "plan"

        mocker.patch.object(TerraformCommandConfig, "get_params", return_value="params")
        mocker.patch(
            "tfworker.util.system.pipe_exec",
            return_value=(2, b"stdout", b"stderr"),
        )
        aggregate = mocker.patch(
            "tfworker.commands.terraform.log.log_subprocess_result"
        )

        cmd._run("def", TerraformAction.APPLY)

        aggregate.assert_called_once_with(
            command="terraform apply",
            exit_code=2,
            stdout=b"stdout",
            stderr=b"stderr",
            level=log.LogLevel.ERROR,  # Exit code 2 for apply should be ERROR
            extra={
                "definition": "def",
                "terraform_action": "apply",
            },
            message="terraform apply output for def",
        )
        log.log_format = old_format

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
        """A hook failure is reported to the caller, it does not end the run."""
        cmd = make_command(tmp_path)
        defn = cmd.app_state.definitions["def"]
        mocker.patch("tfworker.commands.terraform.hooks.check_hooks", return_value=True)
        mocker.patch(
            "tfworker.commands.terraform.hooks.hook_exec", side_effect=HookError("boom")
        )

        assert cmd._exec_hook(defn, TerraformAction.APPLY, TerraformStage.PRE) is False
        cmd.ctx.exit.assert_not_called()

    def test_exec_hook_success(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        defn = cmd.app_state.definitions["def"]
        mocker.patch("tfworker.commands.terraform.hooks.check_hooks", return_value=True)
        mocker.patch("tfworker.commands.terraform.hooks.hook_exec")

        assert cmd._exec_hook(defn, TerraformAction.APPLY, TerraformStage.PRE) is True

    def test_exec_terraform_action_flow(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        definition = cmd.app_state.definitions["def"]
        plan_file = tmp_path / "plan.tfplan"
        plan_file.touch()
        definition.plan_file = str(plan_file)
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

    def test_exec_terraform_action_dispatches_error_stage_on_failure(
        self, tmp_path, mocker
    ):
        cmd = make_command(tmp_path)
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(1, b"", b"boom"))
        h = cmd.app_state.handlers
        with pytest.raises(SystemExit):
            cmd._exec_terraform_action("def", TerraformAction.APPLY)
        cmd.ctx.exit.assert_called_with(1)
        # POST is skipped on failure; ERROR must be dispatched with the result
        error_calls = [
            c
            for c in h.exec_handlers.call_args_list
            if c.kwargs.get("stage") == TerraformStage.ERROR
        ]
        assert len(error_calls) == 1
        assert error_calls[0].kwargs["result"].exit_code == 1

    def test_exec_error_handlers_swallows_handler_errors(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        cmd.app_state.handlers.exec_handlers.side_effect = HandlerError("nope")
        # Must not raise — error-stage handler failures cannot mask the original error
        cmd._exec_error_handlers(
            "def", TerraformAction.APPLY, TerraformResult(1, b"", b"")
        )

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

        cmd.ctx.exit.reset_mock()
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(1, b"", b""))
        cmd._exec_terraform_plan("def")
        assert cmd.app_state.definitions["def"].plan_failed is True
        cmd.ctx.exit.assert_not_called()

        cmd.app_state.definitions["def"].plan_failed = False
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(2, b"", b""))
        cmd._exec_terraform_plan("def")
        gen.assert_called()
        assert cmd.app_state.definitions["def"].needs_apply

        cmd.app_state.definitions["def"].plan_failed = False
        cmd.app_state.handlers.exec_handlers.side_effect = HandlerError("h")
        with pytest.raises(SystemExit):
            cmd._exec_terraform_plan("def")
        cmd.ctx.exit.assert_called_with(2)

    def test_exec_terraform_plan_error_dispatches_error_stage(self, tmp_path, mocker):
        """On plan exit 1, the ERROR stage is dispatched with the failing result."""
        cmd = make_command(tmp_path)
        defn = cmd.app_state.definitions["def"]
        defn.plan_file = tmp_path / "plan.tfplan"
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(1, b"error", b""))
        mocker.patch.object(cmd, "_exec_hook")

        cmd._exec_terraform_plan("def")

        cmd.app_state.handlers.exec_handlers.assert_called_once()
        call_kwargs = cmd.app_state.handlers.exec_handlers.call_args.kwargs
        assert call_kwargs["stage"] == TerraformStage.ERROR
        assert call_kwargs["result"].exit_code == 1
        assert defn.plan_failed is True

    def test_exec_terraform_plan_error_skips_post_hooks(self, tmp_path, mocker):
        """POST hooks must NOT be called when plan exits with code 1."""
        cmd = make_command(tmp_path)
        defn = cmd.app_state.definitions["def"]
        defn.plan_file = tmp_path / "plan.tfplan"
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(1, b"error", b""))
        hook_mock = mocker.patch.object(cmd, "_exec_hook")

        cmd._exec_terraform_plan("def")

        hook_mock.assert_not_called()

    def _make_two_def_command(self, tmp_path, **opts):
        """Helper: make_command with a second definition added."""
        cmd = make_command(tmp_path, plan=True, **opts)
        cmd.app_state.definitions["def2"] = Definition(name="def2", path="module2")
        return cmd

    def _patch_plan_loop(self, cmd, mocker, fail_names):
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_inst = plan_cls.return_value
        plan_inst.needs_plan.return_value = (True, "reason")
        cmd._exec_terraform_pre_plan = mocker.Mock()

        def mark(name):
            cmd.app_state.definitions[name].plan_failed = name in fail_names

        cmd._exec_terraform_plan = mocker.Mock(side_effect=mark)

    def test_terraform_plan_failures_true_fail_true_halts_and_exits(
        self, tmp_path, mocker
    ):
        """plan_failures=True, fail_on_plan_error=True: halts loop, exits 1."""
        cmd = self._make_two_def_command(
            tmp_path, plan_failures=True, fail_on_plan_error=True
        )
        self._patch_plan_loop(cmd, mocker, {"def"})

        with pytest.raises(SystemExit):
            cmd.terraform_plan()

        assert cmd._exec_terraform_plan.call_count == 1
        cmd.ctx.exit.assert_called_with(1)

    def test_terraform_plan_failures_true_fail_false_halts_no_exit(
        self, tmp_path, mocker
    ):
        """plan_failures=True, fail_on_plan_error=False: halts loop, no exit."""
        cmd = self._make_two_def_command(
            tmp_path, plan_failures=True, fail_on_plan_error=False
        )
        self._patch_plan_loop(cmd, mocker, {"def"})

        cmd.terraform_plan()

        assert cmd._exec_terraform_plan.call_count == 1
        cmd.ctx.exit.assert_not_called()

    def test_terraform_plan_failures_false_fail_true_continues_then_exits(
        self, tmp_path, mocker
    ):
        """plan_failures=False, fail_on_plan_error=True: plans all defs, exits 1 at end."""
        cmd = self._make_two_def_command(
            tmp_path, plan_failures=False, fail_on_plan_error=True
        )
        self._patch_plan_loop(cmd, mocker, {"def"})

        with pytest.raises(SystemExit):
            cmd.terraform_plan()

        assert cmd._exec_terraform_plan.call_count == 2
        cmd.ctx.exit.assert_called_with(1)

    def test_terraform_plan_failures_false_fail_false_continues_no_exit(
        self, tmp_path, mocker
    ):
        """plan_failures=False, fail_on_plan_error=False: plans all defs, no exit."""
        cmd = self._make_two_def_command(
            tmp_path, plan_failures=False, fail_on_plan_error=False
        )
        self._patch_plan_loop(cmd, mocker, {"def"})

        cmd.terraform_plan()

        assert cmd._exec_terraform_plan.call_count == 2
        cmd.ctx.exit.assert_not_called()

    def test_terraform_plan_always_apply_skipped_on_plan_failure(
        self, tmp_path, mocker
    ):
        """always_apply must not trigger apply when plan_failed=True."""
        cmd = make_command(
            tmp_path, plan=True, plan_failures=False, fail_on_plan_error=False
        )
        cmd.app_state.definitions["def"].always_apply = True
        self._patch_plan_loop(cmd, mocker, {"def"})
        act = mocker.patch.object(cmd, "_exec_terraform_action")

        cmd.terraform_plan()

        act.assert_not_called()

    def test_terraform_plan_skipped_logs_at_info(self, tmp_path, mocker):
        """A run that plans nothing says so at info; debug would hide a no-op."""
        cmd = make_command(tmp_path, plan=False, plan_destroy=False)
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (
            True,
            "no saved plans possible",
        )
        info = mocker.patch("tfworker.util.log.info")

        cmd.terraform_plan()

        assert any(
            "no plan requested" in str(c.args[0]) for c in info.call_args_list
        ), info.call_args_list

    def test_terraform_apply_or_destroy_skipped_logs_at_info(self, tmp_path, mocker):
        """Same for the apply phase, which is the last thing a run would do."""
        cmd = make_command(tmp_path, apply=False, destroy=False)
        info = mocker.patch("tfworker.util.log.info")

        cmd.terraform_apply_or_destroy()

        assert any(
            "no apply or destroy requested" in str(c.args[0])
            for c in info.call_args_list
        ), info.call_args_list

    def test_terraform_apply_or_destroy(self, tmp_path, mocker):
        cmd = make_command(tmp_path, apply=True)
        cmd.app_state.definitions["def"].needs_apply = True
        plan_file = tmp_path / "plan.tfplan"
        plan_file.touch()
        cmd.app_state.definitions["def"].plan_file = str(plan_file)
        run = mocker.patch.object(
            cmd, "_exec_terraform_action", return_value=TerraformResult(0, b"", b"")
        )
        cmd.terraform_apply_or_destroy()
        run.assert_called_once()

        cmd = make_command(tmp_path, destroy=True, limit=["skip"])
        cmd.app_state.definitions["def"].needs_apply = True
        run = mocker.patch.object(cmd, "_exec_terraform_action")
        cmd.terraform_apply_or_destroy()
        run.assert_not_called()

    def test_terraform_apply_or_destroy_no_plan_file(self, tmp_path, mocker):
        cmd = make_command(tmp_path, apply=True)
        cmd.app_state.definitions["def"].needs_apply = True
        cmd.app_state.definitions["def"].plan_file = None
        run = mocker.patch.object(cmd, "_exec_terraform_action")
        cmd.terraform_apply_or_destroy()
        run.assert_not_called()

    def test_terraform_apply_or_destroy_missing_plan_file(self, tmp_path, mocker):
        cmd = make_command(tmp_path, apply=True)
        cmd.app_state.definitions["def"].needs_apply = True
        cmd.app_state.definitions["def"].plan_file = str(tmp_path / "missing.tfplan")
        run = mocker.patch.object(cmd, "_exec_terraform_action")
        cmd.terraform_apply_or_destroy()
        run.assert_not_called()

    def test_fetch_saved_plans_retrieves_and_marks_apply(self, tmp_path, mocker):
        """An apply-only run pulls the saved plan the plan phase would have."""
        cmd = make_command(tmp_path, apply=True, backend_plans=True, run_id="run1")
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (
            False,
            "plan available from handler",
        )
        cmd.app_state.handlers.get_available_plan.return_value = True

        cmd.terraform_plan()

        cmd.app_state.handlers.get_available_plan.assert_called_once_with(
            cmd.app_state.definitions["def"]
        )
        assert cmd.app_state.definitions["def"].needs_apply is True

    def test_fetch_saved_plans_skips_pre_plan_stage(self, tmp_path, mocker):
        """Scanning handlers hook pre-plan; a run that does not plan must not fire them."""
        cmd = make_command(tmp_path, apply=True, backend_plans=True, run_id="run1")
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (
            False,
            "plan available from handler",
        )
        cmd.app_state.handlers.get_available_plan.return_value = True

        cmd.terraform_plan()

        cmd.app_state.handlers.exec_handlers.assert_not_called()

    def test_fetch_saved_plans_unusable_plan_is_not_applied(self, tmp_path, mocker):
        """A plan whose lineage no longer matches the state is refused, not applied."""
        cmd = make_command(tmp_path, apply=True, backend_plans=True, run_id="run1")
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (
            False,
            "plan available from handler",
        )
        cmd.app_state.handlers.get_available_plan.return_value = False

        cmd.terraform_plan()

        assert cmd.app_state.definitions["def"].needs_apply is False

    def test_fetch_saved_plans_handler_error_is_a_plan_failure(self, tmp_path, mocker):
        """A fetch failure obeys fail_on_plan_error rather than exiting outright."""
        cmd = make_command(
            tmp_path,
            apply=True,
            backend_plans=True,
            run_id="run1",
            fail_on_plan_error=True,
        )
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (
            False,
            "plan available from handler",
        )
        cmd.app_state.handlers.get_available_plan.side_effect = HandlerError("boom")

        with pytest.raises(SystemExit):
            cmd.terraform_plan()

        assert cmd.app_state.definitions["def"].plan_failed is True
        assert cmd.app_state.definitions["def"].needs_apply is False

    def test_fetch_saved_plans_handler_error_tolerated(self, tmp_path, mocker):
        """With the failure options relaxed the run carries on to the apply."""
        cmd = make_command(
            tmp_path,
            apply=True,
            backend_plans=True,
            run_id="run1",
            plan_failures=False,
            fail_on_plan_error=False,
        )
        cmd.app_state.definitions["other"] = Definition(
            name="other", path="module-other"
        )
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (
            False,
            "plan available from handler",
        )
        cmd.app_state.handlers.get_available_plan.side_effect = [
            HandlerError("boom"),
            True,
        ]

        cmd.terraform_plan()

        # the failure is recorded, and the definition behind it still applies
        assert cmd.app_state.definitions["def"].needs_apply is False
        assert cmd.app_state.definitions["other"].needs_apply is True
        cmd.ctx.exit.assert_not_called()

    def test_fetch_saved_plans_halts_on_failure_when_asked(self, tmp_path, mocker):
        """plan_failures stops the fetch, as it stops the plan phase."""
        cmd = make_command(
            tmp_path,
            apply=True,
            backend_plans=True,
            run_id="run1",
            plan_failures=True,
            fail_on_plan_error=False,
        )
        cmd.app_state.definitions["other"] = Definition(
            name="other", path="module-other"
        )
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (
            False,
            "plan available from handler",
        )
        cmd.app_state.handlers.get_available_plan.side_effect = [
            HandlerError("boom"),
            True,
        ]

        cmd.terraform_plan()

        assert cmd.app_state.handlers.get_available_plan.call_count == 1
        assert cmd.app_state.definitions["other"].needs_apply is False

    def test_fetch_saved_plans_no_plan_available(self, tmp_path, mocker):
        cmd = make_command(tmp_path, apply=True, backend_plans=True)
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (True, "no plan file")
        cmd.app_state.definitions["def"].needs_apply = True

        cmd.terraform_plan()

        cmd.app_state.handlers.get_available_plan.assert_not_called()
        assert cmd.app_state.definitions["def"].needs_apply is False

    def test_fetch_saved_plans_local_plan_file_needs_no_fetch(self, tmp_path, mocker):
        cmd = make_command(tmp_path, apply=True, backend_plans=True)
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (False, "plan file exists")

        cmd.terraform_plan()

        cmd.app_state.handlers.get_available_plan.assert_not_called()
        assert cmd.app_state.definitions["def"].needs_apply is True

    def test_fetch_saved_plans_local_plan_file_path_workflow(self, tmp_path, mocker):
        """
        --plan-file-path without --backend-plans: the local plan is the only
        source, and no handler is consulted for it.
        """
        cmd = make_command(
            tmp_path, apply=True, backend_plans=False, plan_file_path=str(tmp_path)
        )
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (False, "plan file exists")

        cmd.terraform_plan()

        assert cmd.app_state.definitions["def"].needs_apply is True
        cmd.app_state.handlers.get_available_plan.assert_not_called()

    def test_fetch_saved_plans_local_plan_file_path_without_a_plan(
        self, tmp_path, mocker
    ):
        """The same options, but nothing was planned; nothing is applied."""
        cmd = make_command(
            tmp_path, apply=True, backend_plans=False, plan_file_path=str(tmp_path)
        )
        cmd.app_state.definitions["def"].needs_apply = True
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (True, "no plan file")

        cmd.terraform_plan()

        assert cmd.app_state.definitions["def"].needs_apply is False
        cmd.app_state.handlers.get_available_plan.assert_not_called()

    def test_fetch_saved_plans_requires_saved_plan_storage(self, tmp_path, mocker):
        """Without --backend-plans or a plan file path there is nothing to fetch."""
        cmd = make_command(tmp_path, apply=True, backend_plans=False)
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (
            True,
            "no saved plans possible",
        )

        cmd.terraform_plan()

        cmd.app_state.handlers.get_available_plan.assert_not_called()

    def test_fetch_saved_plans_skipped_when_not_applying(self, tmp_path, mocker):
        cmd = make_command(tmp_path, apply=False, destroy=False, backend_plans=True)
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (
            False,
            "plan available from handler",
        )

        cmd.terraform_plan()

        cmd.app_state.handlers.get_available_plan.assert_not_called()

    def test_terraform_init(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        dp = mocker.patch("tfworker.definitions.prepare.DefinitionPrepare")
        run = mocker.patch.object(
            cmd, "_exec_terraform_action", return_value=TerraformResult(0, b"", b"")
        )
        cmd.terraform_init()
        assert dp.called
        assert run.called
        assert cmd.app_state.definitions["def"].init_failed is False

    def test_terraform_init_sequential_small(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        prepare_mock = mocker.patch.object(cmd, "_prepare_definition")
        init_mock = mocker.patch.object(
            cmd, "_terraform_init_single", return_value=TerraformResult(0, b"", b"")
        )
        cmd.terraform_init()
        assert prepare_mock.call_count == 1
        assert init_mock.call_count == 1

    def test_terraform_init_parallel_large(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        for i in range(2, 5):
            cmd.app_state.definitions[f"def{i}"] = cmd.app_state.definitions["def"]
        prepare_mock = mocker.patch.object(cmd, "_prepare_definition")
        init_mock = mocker.patch.object(
            cmd, "_terraform_init_single", return_value=TerraformResult(0, b"", b"")
        )
        cmd.terraform_init()
        assert prepare_mock.call_count == 4
        assert init_mock.call_count == 4

    def test_terraform_init_proceeds_no_local_plan_and_handler_has_plan(
        self, tmp_path, mocker
    ):
        """Test that init proceeds when --no-plan is set and handler has plan available"""
        cmd = make_command(tmp_path, plan=False, plan_file_path=None)  # --no-plan

        # Mock handlers collection to indicate plan is available
        mock_handlers = mocker.MagicMock()
        mock_handlers.has_available_plan.return_value = True
        cmd.app_state.handlers = mock_handlers

        # Mock existing_planfile method on Definition class
        mocker.patch(
            "tfworker.definitions.model.Definition.existing_planfile",
            return_value=False,
        )

        prepare_mock = mocker.patch.object(cmd, "_prepare_definition")
        init_mock = mocker.patch.object(
            cmd, "_terraform_init_single", return_value=TerraformResult(0, b"", b"")
        )

        cmd.terraform_init()

        # Should prepare and init since handler has plan to apply
        assert prepare_mock.call_count == 1
        assert init_mock.call_count == 1
        assert cmd.app_state.definitions["def"].needs_apply is True

    def test_terraform_init_proceeds_with_local_plan_and_no_handler_plan(
        self, tmp_path, mocker
    ):
        """Test that init proceeds when --no-plan is set and local plan file exists"""
        cmd = make_command(tmp_path, plan=False, plan_file_path=None)  # --no-plan

        # Mock handlers collection to indicate no plan available
        mock_handlers = mocker.MagicMock()
        mock_handlers.has_available_plan.return_value = False
        cmd.app_state.handlers = mock_handlers

        # Mock existing_planfile to return True (local plan exists)
        mocker.patch(
            "tfworker.definitions.model.Definition.existing_planfile", return_value=True
        )

        prepare_mock = mocker.patch.object(cmd, "_prepare_definition")
        init_mock = mocker.patch.object(
            cmd, "_terraform_init_single", return_value=TerraformResult(0, b"", b"")
        )

        cmd.terraform_init()

        # Should prepare and init since local plan exists to apply
        assert prepare_mock.call_count == 1
        assert init_mock.call_count == 1
        assert cmd.app_state.definitions["def"].needs_apply is True

    def test_terraform_init_skipped_no_local_plan_and_no_handler_plan(
        self, tmp_path, mocker
    ):
        """Test that init is skipped when --no-plan is set and no plans are available"""
        cmd = make_command(tmp_path, plan=False, plan_file_path=None)  # --no-plan

        # Mock handlers collection to indicate no plan available
        mock_handlers = mocker.MagicMock()
        mock_handlers.has_available_plan.return_value = False
        cmd.app_state.handlers = mock_handlers

        # Mock existing_planfile to return False (no local plan)
        mocker.patch(
            "tfworker.definitions.model.Definition.existing_planfile",
            return_value=False,
        )

        prepare_mock = mocker.patch.object(cmd, "_prepare_definition")
        init_mock = mocker.patch.object(
            cmd, "_terraform_init_single", return_value=TerraformResult(0, b"", b"")
        )

        cmd.terraform_init()

        # Should not prepare or init since no plans are available (optimization)
        assert prepare_mock.call_count == 0
        assert init_mock.call_count == 0

    def test_terraform_init_always_proceeds_when_plan_enabled(self, tmp_path, mocker):
        """Test that init always proceeds when planning is enabled (default behavior)"""
        cmd = make_command(tmp_path, plan=True, plan_file_path=None)  # planning enabled

        # Mock handlers collection to indicate plan is available
        mock_handlers = mocker.MagicMock()
        mock_handlers.has_available_plan.return_value = True
        cmd.app_state.handlers = mock_handlers

        # Mock existing_planfile to return True (local plan exists)
        mocker.patch(
            "tfworker.definitions.model.Definition.existing_planfile", return_value=True
        )

        prepare_mock = mocker.patch.object(cmd, "_prepare_definition")
        init_mock = mocker.patch.object(
            cmd, "_terraform_init_single", return_value=TerraformResult(0, b"", b"")
        )

        cmd.terraform_init()

        # Should prepare and init even though plans exist, because planning is enabled
        assert prepare_mock.call_count == 1
        assert init_mock.call_count == 1


class TestTerraformInitFailures:
    """Preparation and init failures: reporting, halting, and exit codes."""

    def _make_command(self, tmp_path, count, **opts):
        cmd = make_command(tmp_path, **opts)
        cmd.app_state.definitions = {
            f"def{i}": Definition(name=f"def{i}", path=f"module{i}")
            for i in range(count)
        }
        return cmd

    def _patch_init(self, cmd, mocker, prepare_failures=(), init_failures=()):
        """Patch prepare/init to fail for the named definitions."""

        def prepare(def_prep, name):
            if name in prepare_failures:
                raise TFWorkerException(f"preparation failed: bad module in {name}")

        def init(name):
            return TerraformResult(1 if name in init_failures else 0, b"", b"")

        return (
            mocker.patch.object(cmd, "_prepare_definition", side_effect=prepare),
            mocker.patch.object(cmd, "_terraform_init_single", side_effect=init),
        )

    def test_sequential_prepare_failure_halts_and_exits(self, tmp_path, mocker):
        """Default options: stop at the first failure, skip the rest, exit 1."""
        cmd = self._make_command(tmp_path, 3)
        prepare, init = self._patch_init(cmd, mocker, prepare_failures={"def0"})

        with pytest.raises(SystemExit):
            cmd.terraform_init()

        cmd.ctx.exit.assert_called_with(1)
        assert prepare.call_count == 1
        assert init.call_count == 0
        assert cmd.app_state.definitions["def0"].init_failed is True
        assert cmd.app_state.definitions["def1"].init_skipped is True
        assert cmd.app_state.definitions["def2"].init_skipped is True

    def test_sequential_collects_every_failure(self, tmp_path, mocker):
        """--no-init-failures: attempt all definitions, report all failures."""
        cmd = self._make_command(tmp_path, 3, init_failures=False)
        prepare, init = self._patch_init(
            cmd, mocker, prepare_failures={"def0"}, init_failures={"def2"}
        )

        with pytest.raises(SystemExit):
            cmd.terraform_init()

        cmd.ctx.exit.assert_called_with(1)
        assert prepare.call_count == 3
        # def0 never prepared, so init was only attempted for def1 and def2
        assert init.call_count == 2
        assert cmd.app_state.definitions["def0"].init_failed is True
        assert cmd.app_state.definitions["def1"].init_failed is False
        assert cmd.app_state.definitions["def2"].init_failed is True
        assert not any(d.init_skipped for d in cmd.app_state.definitions.values())
        assert set(cmd.init_errors) == {"def0", "def2"}

    def test_no_fail_on_init_error_continues_run(self, tmp_path, mocker):
        """--no-fail-on-init-error: failures are recorded, the run continues."""
        cmd = self._make_command(
            tmp_path, 2, init_failures=False, fail_on_init_error=False
        )
        self._patch_init(cmd, mocker, prepare_failures={"def0"})

        cmd.terraform_init()

        cmd.ctx.exit.assert_not_called()
        assert cmd.app_state.definitions["def0"].init_failed is True

    def test_prepare_failure_dispatches_error_stage(self, tmp_path, mocker):
        """Handlers see a failed init, not a definition that never ran."""
        cmd = self._make_command(tmp_path, 1, fail_on_init_error=False)
        self._patch_init(cmd, mocker, prepare_failures={"def0"})

        cmd.terraform_init()

        error_calls = [
            c
            for c in cmd.app_state.handlers.exec_handlers.call_args_list
            if c.kwargs.get("stage") == TerraformStage.ERROR
        ]
        assert len(error_calls) == 1
        assert error_calls[0].kwargs["action"] == TerraformAction.INIT
        assert error_calls[0].kwargs["result"].exit_code == 1
        assert b"bad module in def0" in error_calls[0].kwargs["result"].stderr

    def test_init_failure_does_not_dispatch_error_stage_twice(self, tmp_path, mocker):
        """A terraform init failure is dispatched once, by _exec_terraform_action."""
        cmd = self._make_command(tmp_path, 1, fail_on_init_error=False)
        mocker.patch.object(cmd, "_prepare_definition")
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(1, b"", b"boom"))
        mocker.patch.object(cmd, "_exec_hook")

        cmd.terraform_init()

        error_calls = [
            c
            for c in cmd.app_state.handlers.exec_handlers.call_args_list
            if c.kwargs.get("stage") == TerraformStage.ERROR
        ]
        assert len(error_calls) == 1
        assert cmd.app_state.definitions["def0"].init_failed is True
        # the init phase, not _exec_terraform_action, owns the exit code
        cmd.ctx.exit.assert_not_called()

    def test_parallel_prepare_collects_all_failures(self, tmp_path, mocker):
        """Every definition is prepared, so one run reports every bad module."""
        cmd = self._make_command(tmp_path, 5)
        prepare, init = self._patch_init(cmd, mocker, prepare_failures={"def1", "def3"})

        with pytest.raises(SystemExit):
            cmd.terraform_init()

        assert prepare.call_count == 5
        # halting on failure means terraform init is not run at all
        assert init.call_count == 0
        assert set(cmd.init_errors) == {"def1", "def3"}
        assert cmd.app_state.definitions["def0"].init_skipped is True
        cmd.ctx.exit.assert_called_with(1)

    def test_parallel_init_failures_recorded(self, tmp_path, mocker):
        """Parallel terraform init failures are recorded per definition."""
        cmd = self._make_command(tmp_path, 4, fail_on_init_error=False)
        prepare, init = self._patch_init(cmd, mocker, init_failures={"def2"})

        cmd.terraform_init()

        assert prepare.call_count == 4
        assert init.call_count == 4
        assert cmd.app_state.definitions["def2"].init_failed is True
        assert cmd.app_state.definitions["def0"].init_failed is False
        cmd.ctx.exit.assert_not_called()

    def test_parallel_prepare_exception_recorded(self, tmp_path, mocker):
        """A non tfworker exception from a worker thread is captured, not raised."""
        cmd = self._make_command(tmp_path, 4, init_failures=False)
        mocker.patch.object(
            cmd, "_prepare_definition", side_effect=OSError("disk on fire")
        )
        mocker.patch.object(
            cmd, "_terraform_init_single", return_value=TerraformResult(0, b"", b"")
        )

        with pytest.raises(SystemExit):
            cmd.terraform_init()

        assert len(cmd.init_errors) == 4
        assert "disk on fire" in cmd.init_errors["def0"]

    def test_parallel_init_exception_recorded(self, tmp_path, mocker):
        """An exception raised by a parallel init worker is recorded."""
        cmd = self._make_command(tmp_path, 4, fail_on_init_error=False)
        mocker.patch.object(cmd, "_prepare_definition")
        mocker.patch.object(
            cmd,
            "_terraform_init_single",
            side_effect=lambda name: (
                TerraformResult(0, b"initialized\n", b"")
                if name != "def1"
                else (_ for _ in ()).throw(TFWorkerException("init blew up"))
            ),
        )
        info = mocker.patch("tfworker.util.log.info")

        cmd.terraform_init()

        assert cmd.app_state.definitions["def1"].init_failed is True
        assert "init blew up" in cmd.init_errors["def1"]
        # successful output is still logged for the definitions that worked
        info.assert_any_call("[def0] initialized")

    def test_sequential_init_exception_recorded(self, tmp_path, mocker):
        """An exception from terraform init is recorded, not raised."""
        cmd = self._make_command(tmp_path, 1, fail_on_init_error=False)
        mocker.patch.object(cmd, "_prepare_definition")
        mocker.patch.object(
            cmd, "_terraform_init_single", side_effect=TFWorkerException("no binary")
        )

        cmd.terraform_init()

        assert cmd.app_state.definitions["def0"].init_failed is True
        assert "no binary" in cmd.init_errors["def0"]

    def test_click_exit_from_prepare_is_not_captured(self, tmp_path, mocker):
        """A handler/hook exit must not be reinterpreted as an init failure."""
        cmd = self._make_command(tmp_path, 1)
        mocker.patch.object(
            cmd, "_prepare_definition", side_effect=click.exceptions.Exit(2)
        )

        with pytest.raises(click.exceptions.Exit):
            cmd.terraform_init()

        assert cmd.app_state.definitions["def0"].init_failed is False

    def test_click_exit_from_init_is_not_captured(self, tmp_path, mocker):
        """Sequential init: a handler/hook exit propagates untouched."""
        cmd = self._make_command(tmp_path, 1)
        mocker.patch.object(cmd, "_prepare_definition")
        mocker.patch.object(
            cmd, "_terraform_init_single", side_effect=click.exceptions.Exit(2)
        )

        with pytest.raises(click.exceptions.Exit):
            cmd.terraform_init()

        assert cmd.app_state.definitions["def0"].init_failed is False

    def test_click_exit_from_parallel_init_is_not_captured(self, tmp_path, mocker):
        """Parallel init: a handler/hook exit propagates untouched."""
        cmd = self._make_command(tmp_path, 4)
        mocker.patch.object(cmd, "_prepare_definition")
        mocker.patch.object(
            cmd, "_terraform_init_single", side_effect=click.exceptions.Exit(2)
        )

        with pytest.raises(click.exceptions.Exit):
            cmd.terraform_init()

    def test_plan_and_apply_skip_uninitialized_definitions(self, tmp_path, mocker):
        """A definition that failed or skipped init is not planned or applied."""
        cmd = self._make_command(tmp_path, 2, plan=True, apply=True)
        cmd.app_state.definitions["def0"].init_failed = True
        cmd.app_state.definitions["def1"].init_skipped = True
        for defn in cmd.app_state.definitions.values():
            defn.needs_apply = True
            defn.plan_file = str(tmp_path / "plan.tfplan")
        Path(tmp_path / "plan.tfplan").touch()

        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (True, "reason")
        pre_plan = mocker.patch.object(cmd, "_exec_terraform_pre_plan")
        exec_plan = mocker.patch.object(cmd, "_exec_terraform_plan")
        action = mocker.patch.object(cmd, "_exec_terraform_action")

        cmd.terraform_plan()
        cmd.terraform_apply_or_destroy()

        pre_plan.assert_not_called()
        exec_plan.assert_not_called()
        action.assert_not_called()


class TestHookFailures:
    """A hook failure is a failure of its phase, governed by that phase's options."""

    def _fail_hooks(self, cmd, mocker, when=None):
        """Make hooks fail, optionally only for a given (action, stage)."""

        def exec_hook(definition, action, stage, result=None):
            if when is None or when == (action, stage):
                return False
            return True

        return mocker.patch.object(cmd, "_exec_hook", side_effect=exec_hook)

    # ------------------------------------------------------------------ init
    def test_init_pre_hook_failure_is_an_init_failure(self, tmp_path, mocker):
        cmd = make_command(tmp_path, init_failures=False, fail_on_init_error=False)
        cmd.app_state.definitions["def2"] = Definition(name="def2", path="module2")
        mocker.patch.object(cmd, "_prepare_definition")
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(0, b"", b""))
        self._fail_hooks(cmd, mocker, when=(TerraformAction.INIT, TerraformStage.PRE))

        cmd.terraform_init()

        for name in ("def", "def2"):
            assert cmd.app_state.definitions[name].init_failed is True
            assert cmd.init_errors[name] == "pre-init hook failed"
        # the run continues; the phase decides the exit code
        cmd.ctx.exit.assert_not_called()

    def test_init_hook_failure_skips_terraform(self, tmp_path, mocker):
        """A failed pre-init hook must not be followed by terraform init."""
        cmd = make_command(tmp_path, fail_on_init_error=False)
        mocker.patch.object(cmd, "_prepare_definition")
        run = mocker.patch.object(cmd, "_run")
        self._fail_hooks(cmd, mocker, when=(TerraformAction.INIT, TerraformStage.PRE))

        cmd.terraform_init()

        run.assert_not_called()

    def test_init_hook_failure_honors_fail_on_init_error(self, tmp_path, mocker):
        cmd = make_command(tmp_path, init_failures=False, fail_on_init_error=True)
        mocker.patch.object(cmd, "_prepare_definition")
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(0, b"", b""))
        self._fail_hooks(cmd, mocker, when=(TerraformAction.INIT, TerraformStage.PRE))

        with pytest.raises(SystemExit):
            cmd.terraform_init()

        # exit 1 from the init phase, not exit 2 from the middle of a hook
        cmd.ctx.exit.assert_called_once_with(1)

    def test_init_post_hook_failure_is_an_init_failure(self, tmp_path, mocker):
        cmd = make_command(tmp_path, fail_on_init_error=False)
        mocker.patch.object(cmd, "_prepare_definition")
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(0, b"", b""))
        self._fail_hooks(cmd, mocker, when=(TerraformAction.INIT, TerraformStage.POST))

        cmd.terraform_init()

        assert cmd.app_state.definitions["def"].init_failed is True
        assert cmd.init_errors["def"] == "post-init hook failed"

    def test_init_hook_failure_dispatches_error_stage(self, tmp_path, mocker):
        cmd = make_command(tmp_path, fail_on_init_error=False)
        mocker.patch.object(cmd, "_prepare_definition")
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(0, b"", b""))
        self._fail_hooks(cmd, mocker, when=(TerraformAction.INIT, TerraformStage.PRE))

        cmd.terraform_init()

        error_calls = [
            c
            for c in cmd.app_state.handlers.exec_handlers.call_args_list
            if c.kwargs.get("stage") == TerraformStage.ERROR
        ]
        assert len(error_calls) == 1
        assert error_calls[0].kwargs["action"] == TerraformAction.INIT
        assert b"pre-init hook failed" in error_calls[0].kwargs["result"].stderr

    def test_init_hook_failure_skips_plan_and_apply(self, tmp_path, mocker):
        cmd = make_command(
            tmp_path,
            plan=True,
            apply=True,
            fail_on_init_error=False,
            fail_on_plan_error=False,
        )
        mocker.patch.object(cmd, "_prepare_definition")
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(0, b"", b""))
        self._fail_hooks(cmd, mocker, when=(TerraformAction.INIT, TerraformStage.PRE))

        cmd.terraform_init()
        assert cmd.app_state.definitions["def"].init_failed is True

        # patched after init so the hook path above ran for real
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (True, "reason")
        pre_plan = mocker.patch.object(cmd, "_exec_terraform_pre_plan")
        action = mocker.patch.object(cmd, "_exec_terraform_action")

        cmd.terraform_plan()
        cmd.terraform_apply_or_destroy()

        pre_plan.assert_not_called()
        action.assert_not_called()

    # ------------------------------------------------------------------ plan
    def test_pre_plan_hook_failure_marks_plan_failed(self, tmp_path, mocker):
        cmd = make_command(
            tmp_path, plan=True, plan_failures=False, fail_on_plan_error=False
        )
        cmd.app_state.definitions["def2"] = Definition(name="def2", path="module2")
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (True, "reason")
        self._fail_hooks(cmd, mocker, when=(TerraformAction.PLAN, TerraformStage.PRE))
        exec_plan = mocker.patch.object(cmd, "_exec_terraform_plan")

        cmd.terraform_plan()

        # both definitions attempted, neither planned, no exit
        assert exec_plan.call_count == 0
        for name in ("def", "def2"):
            assert cmd.app_state.definitions[name].plan_failed is True
            assert cmd.app_state.definitions[name].needs_apply is False
        cmd.ctx.exit.assert_not_called()

    def test_pre_plan_hook_failure_halts_when_plan_failures_set(self, tmp_path, mocker):
        cmd = make_command(
            tmp_path, plan=True, plan_failures=True, fail_on_plan_error=False
        )
        cmd.app_state.definitions["def2"] = Definition(name="def2", path="module2")
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (True, "reason")
        self._fail_hooks(cmd, mocker, when=(TerraformAction.PLAN, TerraformStage.PRE))

        cmd.terraform_plan()

        assert cmd.app_state.definitions["def"].plan_failed is True
        assert cmd.app_state.definitions["def2"].plan_failed is False

    def test_pre_plan_hook_failure_honors_fail_on_plan_error(self, tmp_path, mocker):
        cmd = make_command(
            tmp_path, plan=True, plan_failures=False, fail_on_plan_error=True
        )
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (True, "reason")
        self._fail_hooks(cmd, mocker, when=(TerraformAction.PLAN, TerraformStage.PRE))

        with pytest.raises(SystemExit):
            cmd.terraform_plan()

        cmd.ctx.exit.assert_called_once_with(1)

    def test_post_plan_hook_failure_prevents_apply(self, tmp_path, mocker):
        """A plan with changes whose post hook failed must not be applied."""
        cmd = make_command(tmp_path, plan=True, fail_on_plan_error=False)
        defn = cmd.app_state.definitions["def"]
        defn.plan_file = tmp_path / "plan.tfplan"
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(2, b"", b""))
        mocker.patch.object(cmd, "_generate_plan_output_json")
        self._fail_hooks(cmd, mocker, when=(TerraformAction.PLAN, TerraformStage.POST))

        cmd._exec_terraform_plan("def")

        assert defn.plan_failed is True
        assert defn.needs_apply is False

    # ----------------------------------------------------------------- apply
    def test_apply_hook_failure_exits_when_called_directly(self, tmp_path, mocker):
        """exit_on_error still ends the run on a hook failure."""
        cmd = make_command(tmp_path, apply=True)
        self._fail_hooks(cmd, mocker, when=(TerraformAction.APPLY, TerraformStage.PRE))

        with pytest.raises(SystemExit):
            cmd._exec_terraform_action("def", TerraformAction.APPLY)

        cmd.ctx.exit.assert_called_with(2)
        assert cmd.app_state.definitions["def"].apply_failed is True


class TestApplyFailurePolicy:
    """apply_failures halts the apply phase; fail_on_apply_error sets the exit."""

    NAMES = ("def0", "def1", "def2")

    def _make(self, tmp_path, mocker, fail=(), **opts):
        cmd = make_command(tmp_path, **opts)
        cmd.app_state.definitions = {}
        for name in self.NAMES:
            definition = Definition(name=name, path=name)
            definition.needs_apply = True
            plan_file = tmp_path / f"{name}.tfplan"
            plan_file.touch()
            definition.plan_file = str(plan_file)
            cmd.app_state.definitions[name] = definition
        mocker.patch.object(cmd, "_exec_hook", return_value=True)
        self.ran = []

        def run(name, action):
            self.ran.append(name)
            return TerraformResult(1 if name in fail else 0, b"", b"boom")

        mocker.patch.object(cmd, "_run", side_effect=run)
        return cmd

    def _error_dispatches(self, cmd):
        return [
            c.kwargs["definition"].name
            for c in cmd.app_state.handlers.exec_handlers.call_args_list
            if c.kwargs["stage"] == TerraformStage.ERROR
        ]

    def test_default_halts_and_exits(self, tmp_path, mocker):
        cmd = self._make(tmp_path, mocker, fail={"def1"})

        with pytest.raises(SystemExit):
            cmd.terraform_apply_or_destroy()

        assert self.ran == ["def0", "def1"]
        defs = cmd.app_state.definitions
        assert defs["def1"].apply_failed is True
        assert defs["def2"].apply_skipped is True
        assert defs["def2"].apply_failed is False
        assert defs["def0"].apply_failed is False
        assert defs["def0"].apply_skipped is False
        assert cmd.apply_errors == {"def1": "terraform apply exited 1"}
        assert self._error_dispatches(cmd) == ["def1"]
        cmd.ctx.exit.assert_called_once_with(1)

    def test_last_definition_failing_skips_nothing(self, tmp_path, mocker):
        cmd = self._make(tmp_path, mocker, fail={"def2"})
        warn = mocker.patch("tfworker.util.log.warn")

        with pytest.raises(SystemExit):
            cmd.terraform_apply_or_destroy()

        assert self.ran == list(self.NAMES)
        assert not any(d.apply_skipped for d in cmd.app_state.definitions.values())
        warn.assert_not_called()

    def test_halt_without_fail_on_apply_error(self, tmp_path, mocker):
        cmd = self._make(tmp_path, mocker, fail={"def1"}, fail_on_apply_error=False)

        cmd.terraform_apply_or_destroy()

        assert self.ran == ["def0", "def1"]
        assert cmd.app_state.definitions["def2"].apply_skipped is True
        cmd.ctx.exit.assert_not_called()

    def test_no_apply_failures_attempts_all_then_exits(self, tmp_path, mocker):
        cmd = self._make(tmp_path, mocker, fail={"def0", "def1"}, apply_failures=False)

        with pytest.raises(SystemExit):
            cmd.terraform_apply_or_destroy()

        assert self.ran == ["def0", "def1", "def2"]
        defs = cmd.app_state.definitions
        assert [n for n in self.NAMES if defs[n].apply_failed] == ["def0", "def1"]
        assert not any(d.apply_skipped for d in defs.values())
        assert self._error_dispatches(cmd) == ["def0", "def1"]
        cmd.ctx.exit.assert_called_once_with(1)

    def test_no_apply_failures_reports_every_failure(self, tmp_path, mocker):
        cmd = self._make(tmp_path, mocker, fail={"def0", "def2"}, apply_failures=False)
        error = mocker.patch("tfworker.util.log.error")

        with pytest.raises(SystemExit):
            cmd.terraform_apply_or_destroy()

        messages = [str(c.args[0]) for c in error.call_args_list]
        assert "2 definitions failed to apply:" in messages
        assert "  def0: terraform apply exited 1" in messages
        assert "  def2: terraform apply exited 1" in messages

    def test_no_apply_failures_no_fail_on_apply_error(self, tmp_path, mocker):
        cmd = self._make(
            tmp_path,
            mocker,
            fail={"def1"},
            apply_failures=False,
            fail_on_apply_error=False,
        )

        cmd.terraform_apply_or_destroy()

        assert self.ran == ["def0", "def1", "def2"]
        cmd.ctx.exit.assert_not_called()

    def test_success_does_not_exit(self, tmp_path, mocker):
        cmd = self._make(tmp_path, mocker)

        cmd.terraform_apply_or_destroy()

        assert self.ran == list(self.NAMES)
        cmd.ctx.exit.assert_not_called()

    def test_destroy_follows_the_policy(self, tmp_path, mocker):
        cmd = self._make(tmp_path, mocker, fail={"def0"}, apply=False, destroy=True)

        with pytest.raises(SystemExit):
            cmd.terraform_apply_or_destroy()

        assert self.ran == ["def0"]
        assert cmd.apply_errors == {"def0": "terraform destroy exited 1"}
        assert cmd.app_state.definitions["def1"].apply_skipped is True

    def test_definitions_without_a_plan_are_neither_failed_nor_skipped(
        self, tmp_path, mocker
    ):
        cmd = self._make(tmp_path, mocker, fail={"def0"})
        cmd.app_state.definitions["def2"].needs_apply = False

        with pytest.raises(SystemExit):
            cmd.terraform_apply_or_destroy()

        defs = cmd.app_state.definitions
        assert defs["def1"].apply_skipped is True
        assert defs["def2"].apply_skipped is False
        assert defs["def2"].apply_failed is False

    def test_pre_apply_hook_failure_is_an_apply_failure(self, tmp_path, mocker):
        cmd = self._make(tmp_path, mocker, apply_failures=False)

        def exec_hook(definition, action, stage, result=None):
            return not (definition.name == "def1" and stage == TerraformStage.PRE)

        mocker.patch.object(cmd, "_exec_hook", side_effect=exec_hook)

        with pytest.raises(SystemExit):
            cmd.terraform_apply_or_destroy()

        assert self.ran == ["def0", "def2"]
        assert cmd.apply_errors == {"def1": "pre-apply hook failed"}
        assert self._error_dispatches(cmd) == ["def1"]
        cmd.ctx.exit.assert_called_once_with(1)

    def test_post_apply_hook_failure_halts_by_default(self, tmp_path, mocker):
        cmd = self._make(tmp_path, mocker)

        def exec_hook(definition, action, stage, result=None):
            return not (definition.name == "def0" and stage == TerraformStage.POST)

        mocker.patch.object(cmd, "_exec_hook", side_effect=exec_hook)

        with pytest.raises(SystemExit):
            cmd.terraform_apply_or_destroy()

        assert self.ran == ["def0"]
        assert cmd.apply_errors == {"def0": "post-apply hook failed"}
        assert self._error_dispatches(cmd) == ["def0"]
        # terraform applied, so the plan is consumed
        assert not Path(cmd.app_state.definitions["def0"].plan_file).exists()
        assert cmd.app_state.definitions["def1"].apply_skipped is True
        assert cmd.app_state.definitions["def2"].apply_skipped is True

    def _always_apply_plan(self, cmd, mocker):
        for definition in cmd.app_state.definitions.values():
            definition.always_apply = True
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (True, "reason")
        mocker.patch.object(cmd, "_exec_terraform_pre_plan", return_value=True)
        return mocker.patch.object(cmd, "_exec_terraform_plan")

    def test_always_apply_failure_halts_applies_not_planning(self, tmp_path, mocker):
        cmd = self._make(tmp_path, mocker, fail={"def0"}, plan=True)
        planned = self._always_apply_plan(cmd, mocker)
        cmd.app_state.definitions["def1"].always_apply = False

        cmd.terraform_plan()

        assert [c.kwargs["name"] for c in planned.call_args_list] == list(self.NAMES)
        assert self.ran == ["def0"]
        assert cmd.app_state.definitions["def2"].apply_skipped is True
        cmd.ctx.exit.assert_not_called()

        with pytest.raises(SystemExit):
            cmd.terraform_apply_or_destroy()

        assert self.ran == ["def0"]
        defs = cmd.app_state.definitions
        assert defs["def0"].apply_failed is True
        assert defs["def1"].apply_skipped is True
        cmd.ctx.exit.assert_called_once_with(1)

    def test_always_apply_failure_continues_without_apply_failures(
        self, tmp_path, mocker
    ):
        cmd = self._make(
            tmp_path, mocker, fail={"def0"}, plan=True, apply_failures=False
        )
        self._always_apply_plan(cmd, mocker)

        cmd.terraform_plan()

        assert self.ran == ["def0", "def1", "def2"]
        with pytest.raises(SystemExit):
            cmd.terraform_apply_or_destroy()
        assert self.ran == ["def0", "def1", "def2"]
        cmd.ctx.exit.assert_called_once_with(1)

    def test_always_apply_failure_reported_when_plan_phase_exits(
        self, tmp_path, mocker
    ):
        cmd = self._make(
            tmp_path,
            mocker,
            fail={"def1"},
            plan=True,
            plan_failures=False,
            apply_failures=False,
            fail_on_apply_error=False,
        )
        self._always_apply_plan(cmd, mocker)
        cmd.app_state.definitions["def0"].plan_failed = True
        error = mocker.patch("tfworker.util.log.error")

        with pytest.raises(SystemExit):
            cmd.terraform_plan()

        messages = [str(c.args[0]) for c in error.call_args_list]
        assert "  def1: terraform apply exited 1" in messages
        cmd.ctx.exit.assert_called_once_with(1)

    def test_error_handlers_see_apply_failed(self, tmp_path, mocker):
        cmd = self._make(tmp_path, mocker, fail={"def0"}, apply_failures=False)
        seen = {}

        def exec_handlers(**kwargs):
            if kwargs["stage"] == TerraformStage.ERROR:
                seen[kwargs["definition"].name] = kwargs["definition"].apply_failed

        cmd.app_state.handlers.exec_handlers.side_effect = exec_handlers

        with pytest.raises(SystemExit):
            cmd.terraform_apply_or_destroy()

        assert seen == {"def0": True}

    def test_destroy_limit_with_halt_skips_only_limited(self, tmp_path, mocker):
        cmd = self._make(
            tmp_path,
            mocker,
            fail={"def0"},
            apply=False,
            destroy=True,
            limit=["def0", "def1"],
        )

        with pytest.raises(SystemExit):
            cmd.terraform_apply_or_destroy()

        defs = cmd.app_state.definitions
        assert self.ran == ["def0"]
        assert defs["def1"].apply_skipped is True
        assert defs["def2"].apply_skipped is False
        assert defs["def2"].apply_failed is False

    def test_uninitialized_definitions_are_not_marked_skipped(self, tmp_path, mocker):
        cmd = self._make(tmp_path, mocker, fail={"def0"})
        cmd.app_state.definitions["def2"].init_failed = True

        with pytest.raises(SystemExit):
            cmd.terraform_apply_or_destroy()

        assert cmd.app_state.definitions["def1"].apply_skipped is True
        assert cmd.app_state.definitions["def2"].apply_skipped is False

    def test_always_apply_failure_reported_in_plan_only_run(self, tmp_path, mocker):
        cmd = self._make(tmp_path, mocker, fail={"def0"}, plan=True, apply=False)
        self._always_apply_plan(cmd, mocker)

        cmd.terraform_plan()
        with pytest.raises(SystemExit):
            cmd.terraform_apply_or_destroy()

        assert cmd.apply_errors == {"def0": "terraform apply exited 1"}
        cmd.ctx.exit.assert_called_once_with(1)


class TestGetDefinitionsNeedingInit:
    def test_all_definitions_when_plan_enabled(self, tmp_path, mocker):
        """Test that all definitions are returned when planning is enabled"""
        cmd = make_command(tmp_path, plan=True, plan_file_path=None)
        cmd.app_state.definitions = {"def1": mock.Mock(), "def2": mock.Mock()}

        result = cmd._get_definitions_needing_init()
        assert result == ["def1", "def2"]

    def test_no_plan_with_local_no_local_plan_and_handler_has_plans(
        self, tmp_path, mocker
    ):
        """Test that init is needed when --no-plan is set and plans are available"""
        cmd = make_command(tmp_path, plan=False, plan_file_path=None)
        mock_def = mock.Mock()
        cmd.app_state.definitions = {"def1": mock_def}

        # Mock handlers to indicate plan available
        mock_handlers = mock.Mock()
        mock_handlers.has_available_plan.return_value = True
        cmd.app_state.handlers = mock_handlers

        # Mock existing_planfile to return False
        mock_def.existing_planfile.return_value = False
        mocker.patch("tfworker.definitions.plan.DefinitionPlan.set_plan_file")

        result = cmd._get_definitions_needing_init()
        assert result == ["def1"]
        assert mock_def.needs_apply is True

    def test_no_plan_with_local_plan_and_no_handler_plan(self, tmp_path, mocker):
        """Test that init is needed when --no-plan is set and local plan exists"""
        cmd = make_command(tmp_path, plan=False, plan_file_path=None)
        mock_def = mock.Mock()
        cmd.app_state.definitions = {"def1": mock_def}

        # Mock handlers to indicate no plan available
        mock_handlers = mock.Mock()
        mock_handlers.has_available_plan.return_value = False
        cmd.app_state.handlers = mock_handlers

        # Mock existing_planfile to return True
        mock_def.existing_planfile.return_value = True
        mocker.patch("tfworker.definitions.plan.DefinitionPlan.set_plan_file")

        result = cmd._get_definitions_needing_init()
        assert result == ["def1"]
        assert mock_def.needs_apply is True

    def test_no_plan_with_no_local_plan_and_no_handler_plan(self, tmp_path, mocker):
        """Test that init is skipped when --no-plan is set and no plans are available"""
        cmd = make_command(tmp_path, plan=False, plan_file_path=None)
        mock_def = mock.Mock()
        cmd.app_state.definitions = {"def1": mock_def}

        # Mock handlers to indicate no plan available
        mock_handlers = mock.Mock()
        mock_handlers.has_available_plan.return_value = False
        cmd.app_state.handlers = mock_handlers

        # Mock existing_planfile to return False
        mock_def.existing_planfile.return_value = False
        mocker.patch("tfworker.definitions.plan.DefinitionPlan.set_plan_file")

        result = cmd._get_definitions_needing_init()
        assert result == []

    def test_all_definitions_when_not_apply_mode(self, tmp_path, mocker):
        """Test that all definitions are returned when NOT in apply mode"""
        cmd = make_command(tmp_path, plan=False, apply=False, plan_file_path=None)
        cmd.app_state.definitions = {"def1": mock.Mock(), "def2": mock.Mock()}

        result = cmd._get_definitions_needing_init()
        assert result == ["def1", "def2"]


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
        assert res.stderr_str == "err"

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
        cmd = make_command(tmp_path, plan=True)
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
        cmd = make_command(tmp_path, plan=True)
        cmd.app_state.definitions["def"].always_apply = True
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_inst = plan_cls.return_value
        plan_inst.needs_plan.return_value = (True, "reason")
        cmd._exec_terraform_pre_plan = mocker.Mock()
        _ = mocker.patch.object(cmd, "_exec_terraform_plan")
        act = mocker.patch.object(
            cmd, "_exec_terraform_action", return_value=TerraformResult(0, b"", b"")
        )
        mocker.patch.object(cmd, "_run", return_value=TerraformResult(0, b"", b""))
        cmd.terraform_plan()
        act.assert_called_once()

    def test_terraform_apply_or_destroy_skip(self, tmp_path):
        cmd = make_command(tmp_path)
        # no apply or destroy
        cmd.app_state.terraform_options.apply = False
        cmd.app_state.terraform_options.destroy = False
        cmd.terraform_apply_or_destroy()  # should noop


class TestTerraformResultTiming:
    def test_duration_is_none_when_untimed(self):
        assert TerraformResult(0, b"", b"").duration is None

    def test_duration_from_timestamps(self):
        from datetime import datetime, timedelta, timezone

        start = datetime(2026, 11, 27, 9, 0, tzinfo=timezone.utc)
        result = TerraformResult(
            0, b"", b"", started_at=start, finished_at=start + timedelta(seconds=90)
        )
        assert result.duration == 90.0

    def test_run_records_start_and_finish(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        cmd.app_state.definitions["def"].plan_file = "plan"
        mocker.patch.object(TerraformCommandConfig, "get_params", return_value="")
        mocker.patch("tfworker.util.system.pipe_exec", return_value=(0, b"", b""))

        result = cmd._run("def", TerraformAction.APPLY)

        assert result.started_at is not None
        assert result.finished_at >= result.started_at
        assert result.started_at.tzinfo is not None
        assert result.duration >= 0


class TestSkipNotifications:
    """Handlers hear about every definition a phase passes over (#219)."""

    @staticmethod
    def _skips(cmd):
        return [
            (
                c.kwargs["definition"].name,
                c.kwargs["action"],
                c.kwargs["reason"],
            )
            for c in cmd.app_state.handlers.exec_skip.call_args_list
        ]

    def _two_defs(self, tmp_path, **opts):
        cmd = make_command(tmp_path, **opts)
        cmd.app_state.definitions["def2"] = Definition(name="def2", path="module2")
        return cmd

    def test_plan_not_needed_is_a_skip(self, tmp_path, mocker):
        cmd = make_command(tmp_path, plan=True)
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (False, "plan file exists")
        mocker.patch.object(cmd, "_exec_terraform_pre_plan", return_value=True)

        cmd.terraform_plan()

        assert self._skips(cmd) == [("def", TerraformAction.PLAN, "plan file exists")]
        cmd.app_state.handlers.exec_skip.assert_called_with(
            action=TerraformAction.PLAN,
            deployment="dep",
            definition=cmd.app_state.definitions["def"],
            reason="plan file exists",
        )

    def test_uninitialized_definition_is_a_plan_skip(self, tmp_path, mocker):
        cmd = make_command(tmp_path, plan=True)
        cmd.app_state.definitions["def"].init_failed = True
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (True, "no plan")

        cmd.terraform_plan()

        assert self._skips(cmd) == [("def", TerraformAction.PLAN, "not initialized")]

    def test_plan_halt_skips_the_rest(self, tmp_path, mocker):
        cmd = self._two_defs(
            tmp_path, plan=True, plan_failures=True, fail_on_plan_error=False
        )
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (True, "no plan")
        mocker.patch.object(cmd, "_exec_terraform_pre_plan", return_value=True)

        def fail(name):
            cmd.app_state.definitions[name].plan_failed = True

        mocker.patch.object(cmd, "_exec_terraform_plan", side_effect=fail)

        cmd.terraform_plan()

        assert self._skips(cmd) == [
            ("def2", TerraformAction.PLAN, "plan halted by an earlier failure")
        ]

    def test_pre_plan_halt_skips_the_rest(self, tmp_path, mocker):
        cmd = self._two_defs(
            tmp_path, plan=True, plan_failures=True, fail_on_plan_error=False
        )
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (True, "no plan")
        mocker.patch.object(cmd, "_exec_terraform_pre_plan", return_value=False)

        cmd.terraform_plan()

        assert self._skips(cmd) == [
            ("def2", TerraformAction.PLAN, "plan halted by an earlier failure")
        ]

    def test_no_saved_plan_is_an_apply_skip(self, tmp_path, mocker):
        cmd = make_command(tmp_path, apply=True, backend_plans=True)
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (True, "no plan file")

        cmd.terraform_plan()

        assert self._skips(cmd) == [("def", TerraformAction.APPLY, "no saved plan")]

    def test_no_saved_plan_on_destroy_is_a_destroy_skip(self, tmp_path, mocker):
        cmd = make_command(tmp_path, apply=False, destroy=True, backend_plans=True)
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (True, "no plan file")

        cmd.terraform_plan()

        assert self._skips(cmd) == [("def", TerraformAction.DESTROY, "no saved plan")]

    def test_fetch_halt_skips_the_rest(self, tmp_path, mocker):
        cmd = self._two_defs(
            tmp_path,
            apply=True,
            backend_plans=True,
            plan_failures=True,
            fail_on_plan_error=False,
        )
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (False, "from handler")
        cmd.app_state.handlers.get_available_plan.side_effect = HandlerError("boom")

        cmd.terraform_plan()

        assert self._skips(cmd) == [
            ("def2", TerraformAction.APPLY, "apply halted by an earlier failure")
        ]

    def test_apply_candidates_report_skips(self, tmp_path, mocker):
        cmd = make_command(tmp_path, apply=True)
        defs = cmd.app_state.definitions
        defs["def"].init_failed = True
        defs["missing"] = Definition(name="missing", path="m")
        defs["missing"].needs_apply = True
        defs["missing"].plan_file = str(tmp_path / "nope.tfplan")
        defs["clean"] = Definition(name="clean", path="c")

        assert cmd._apply_candidates(TerraformAction.APPLY) == []
        assert self._skips(cmd) == [
            ("def", TerraformAction.APPLY, "not initialized"),
            ("missing", TerraformAction.APPLY, "plan file does not exist"),
        ]

    def test_destroy_outside_limit_is_a_skip(self, tmp_path, mocker):
        cmd = make_command(tmp_path, apply=False, destroy=True, limit=["other"])
        cmd.app_state.definitions["def"].needs_apply = True

        assert cmd._apply_candidates(TerraformAction.DESTROY) == []
        assert self._skips(cmd) == [("def", TerraformAction.DESTROY, "outside --limit")]

    def test_apply_halt_skips_the_rest(self, tmp_path, mocker):
        cmd = make_command(tmp_path, apply=True)
        cmd._mark_apply_skipped(["def"], TerraformAction.APPLY)

        assert cmd.app_state.definitions["def"].apply_skipped is True
        assert self._skips(cmd) == [
            ("def", TerraformAction.APPLY, "apply halted by an earlier failure")
        ]

    def test_init_halt_skips_only_unattempted(self, tmp_path, mocker):
        cmd = self._two_defs(tmp_path)
        cmd.app_state.definitions["def"].init_failed = True

        cmd._mark_init_skipped(["def", "def2"])

        assert self._skips(cmd) == [
            ("def2", TerraformAction.INIT, "init halted by an earlier failure")
        ]

    def test_nothing_skipped_sends_nothing(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        cmd._mark_init_skipped([])
        cmd._mark_apply_skipped([], TerraformAction.APPLY)
        cmd.app_state.handlers.exec_skip.assert_not_called()


class TestSkipNotificationsAcrossPhases:
    """Each definition is reported once per action, and never alongside an apply."""

    @staticmethod
    def _skips(cmd):
        return [
            (c.kwargs["definition"].name, c.kwargs["action"], c.kwargs["reason"])
            for c in cmd.app_state.handlers.exec_skip.call_args_list
        ]

    def test_uninitialized_definition_in_apply_only_run_is_reported_once(
        self, tmp_path, mocker
    ):
        cmd = make_command(tmp_path, apply=True, backend_plans=True)
        cmd.app_state.definitions["def"].init_failed = True
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (True, "no plan file")

        cmd.terraform_plan()
        cmd.terraform_apply_or_destroy()

        assert self._skips(cmd) == [("def", TerraformAction.APPLY, "not initialized")]
        cmd.app_state.handlers.get_available_plan.assert_not_called()

    def test_fetch_halt_does_not_skip_a_definition_that_still_applies(
        self, tmp_path, mocker
    ):
        cmd = make_command(
            tmp_path,
            apply=True,
            backend_plans=True,
            plan_failures=True,
            fail_on_plan_error=False,
        )
        defs = cmd.app_state.definitions
        defs["local"] = Definition(name="local", path="local")
        defs["remote"] = Definition(name="remote", path="remote")
        local_plan = tmp_path / "local.tfplan"
        local_plan.touch()

        def needs_plan(definition):
            if definition.name == "local":
                definition.plan_file = str(local_plan)
                return (False, "plan file exists")
            return (False, "plan available from handler")

        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.side_effect = needs_plan
        cmd.app_state.handlers.get_available_plan.side_effect = HandlerError("boom")
        ran = mocker.patch.object(
            cmd, "_exec_terraform_action", return_value=TerraformResult(0, b"", b"")
        )

        cmd.terraform_plan()
        cmd.terraform_apply_or_destroy()

        assert self._skips(cmd) == [
            ("remote", TerraformAction.APPLY, "apply halted by an earlier failure")
        ]
        assert [c.kwargs["name"] for c in ran.call_args_list] == ["local"]

    def test_unusable_saved_plan_is_a_skip(self, tmp_path, mocker):
        cmd = make_command(tmp_path, apply=True, backend_plans=True)
        plan_cls = mocker.patch("tfworker.definitions.plan.DefinitionPlan")
        plan_cls.return_value.needs_plan.return_value = (False, "from handler")
        cmd.app_state.handlers.get_available_plan.return_value = False

        cmd.terraform_plan()
        cmd.terraform_apply_or_destroy()

        assert self._skips(cmd) == [
            ("def", TerraformAction.APPLY, "saved plan could not be used")
        ]

    def test_always_apply_halt_is_reported_once(self, tmp_path, mocker):
        cmd = make_command(tmp_path, apply=True)
        definition = cmd.app_state.definitions["def"]
        definition.needs_apply = True
        plan_file = tmp_path / "def.tfplan"
        plan_file.touch()
        definition.plan_file = str(plan_file)
        cmd._apply_halted = True

        cmd._always_apply("def")
        cmd.terraform_apply_or_destroy()

        assert self._skips(cmd) == [
            ("def", TerraformAction.APPLY, "apply halted by an earlier failure")
        ]


class TestSynthesizedResultTiming:
    def test_synthesized_result_is_stamped(self):
        result = TerraformResult.synthesized(2, "pre-plan hook failed")
        assert result.exit_code == 2
        assert result.stderr == b"pre-plan hook failed"
        assert result.started_at is not None
        assert result.finished_at == result.started_at
        assert result.duration == 0

    def test_hook_failure_result_is_stamped(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        result = cmd._hook_failure(
            "def", TerraformAction.PLAN, TerraformStage.PRE, exit_on_error=False
        )
        assert result.started_at is not None

    def test_init_failure_result_is_stamped(self, tmp_path):
        cmd = make_command(tmp_path)
        cmd._record_init_failure("def", "copy failed", dispatch_error_stage=True)
        result = cmd.app_state.handlers.exec_handlers.call_args.kwargs["result"]
        assert result.started_at is not None

    def test_duration_prefers_monotonic_elapsed(self):
        from datetime import datetime, timedelta, timezone

        start = datetime(2026, 11, 27, tzinfo=timezone.utc)
        result = TerraformResult(
            0, b"", b"", started_at=start, finished_at=start - timedelta(seconds=5)
        )
        result.elapsed = 12.5
        assert result.duration == 12.5


class TestLockTimeout:
    @pytest.fixture
    def config_class(self):
        # another test reloads the module, so look the class up at run time
        from tfworker.commands.terraform import TerraformCommandConfig

        TerraformCommandConfig._instance = None
        yield TerraformCommandConfig
        TerraformCommandConfig._instance = None

    @pytest.mark.parametrize(
        "action", [TerraformAction.PLAN, TerraformAction.APPLY, TerraformAction.DESTROY]
    )
    def test_get_params_lock_timeout(self, action, config_class):
        cfg = config_class(DummyAppState())
        assert "-lock-timeout=91s" in cfg.get_params(action, "plan", 90.2)
        assert "-lock-timeout" not in cfg.get_params(action, "plan")

    def test_init_never_gets_lock_timeout(self, config_class):
        cfg = config_class(DummyAppState())
        assert "-lock-timeout" not in cfg.get_params(TerraformAction.INIT, "", 30)

    def test_deadline_overrides_option(self, tmp_path, mocker):
        cmd = make_command(tmp_path, lock_timeout="5m")
        assert cmd._lock_timeout(None) == 300
        mocker.patch("tfworker.commands.terraform.time.monotonic", return_value=100.0)
        assert cmd._lock_timeout(142.0) == 42.0
        assert cmd._lock_timeout(90.0) == 0.0

    def test_no_timeout_by_default(self, tmp_path):
        assert make_command(tmp_path)._lock_timeout(None) is None

    def test_run_passes_lock_timeout(self, tmp_path, mocker):
        cmd = make_command(tmp_path, lock_timeout="2m")
        cmd.app_state.definitions["def"].plan_file = str(tmp_path / "p.tfplan")
        params = mocker.patch.object(
            cmd.terraform_config, "get_params", return_value=""
        )
        mocker.patch(
            "tfworker.commands.terraform.pipe_exec_logged", return_value=(0, b"", b"")
        )
        cmd._run("def", TerraformAction.APPLY)
        assert params.call_args.kwargs["lock_timeout"] == 120


def with_locking(cmd, **options):
    from tfworker.custom_types.config_file import LockingOptions

    cmd.app_state.loaded_config.locking = LockingOptions(**options)
    locker = cmd.state_locker
    locker.acquire = mock.Mock(return_value=500.0)
    locker.release = mock.Mock()
    return locker


class TestStateLocker:
    def test_off_without_locking_config(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        assert cmd.state_locker is None
        run = mocker.patch.object(cmd, "_run", return_value="result")
        assert cmd._run_locked("def", TerraformAction.APPLY) == "result"
        run.assert_called_once_with("def", TerraformAction.APPLY)

    def test_built_from_config(self, tmp_path, mocker):
        from tfworker.custom_types.config_file import LockingOptions

        cmd = make_command(tmp_path, run_id="run-1")
        cmd.app_state.loaded_config.locking = LockingOptions(wait="1m")
        mocker.patch(
            "tfworker.commands.terraform.terraform_env", return_value={"A": "1"}
        )
        locker = cmd.state_locker
        assert locker is cmd.state_locker
        assert locker.options.wait == 60
        assert locker.backend == "backend"
        assert locker._terraform_bin == "/bin/terraform"
        assert locker._run_id == "run-1"
        assert locker._env() == {"A": "1"}

    def test_lock_timeout_option_ignored_with_locking(self, tmp_path, mocker):
        from tfworker.custom_types.config_file import LockingOptions

        cmd = make_command(tmp_path, lock_timeout="5m")
        cmd.app_state.loaded_config.locking = LockingOptions()
        info = mocker.patch("tfworker.commands.terraform.log.info")
        assert cmd.state_locker is not None
        assert "--lock-timeout is ignored" in info.call_args.args[0]

    def test_real_locker_keeps_lease_while_terraform_runs(self, tmp_path, mocker):
        import os

        from tfworker.custom_types.config_file import LockingOptions

        backend = mock.Mock(lock_inspection=True)
        backend.get_lock.return_value = None
        cmd = make_command(tmp_path, run_id="run-1")
        cmd.app_state.backend = backend
        cmd.app_state.loaded_config.locking = LockingOptions(wait="2m")

        def run(name, action, lock_deadline=None):
            lease = backend.put_lease.call_args.args[0]
            assert lease.definition == "def" and lease.run_id == "run-1"
            backend.get_lease.return_value = lease
            backend.delete_lease.assert_not_called()
            assert 119 <= cmd._lock_timeout(lock_deadline) <= 120
            assert lease.pid == os.getpid()
            return "result"

        mocker.patch.object(cmd, "_run", side_effect=run)
        assert cmd._run_locked("def", TerraformAction.APPLY) == "result"
        backend.delete_lease.assert_called_once()

    def test_lock_held_around_terraform(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        locker = with_locking(cmd)
        run = mocker.patch.object(cmd, "_run", return_value="result")
        assert cmd._run_locked("def", TerraformAction.PLAN) == "result"
        locker.acquire.assert_called_once_with(
            TerraformAction.PLAN, "def", tmp_path / "definitions" / "def"
        )
        run.assert_called_once_with("def", TerraformAction.PLAN, lock_deadline=500.0)
        locker.release.assert_called_once_with("def")
        assert cmd.lock_waits["def"] >= 0

    def test_released_when_terraform_raises(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        locker = with_locking(cmd)
        mocker.patch.object(cmd, "_run", side_effect=SystemExit)
        with pytest.raises(SystemExit):
            cmd._run_locked("def", TerraformAction.APPLY)
        locker.release.assert_called_once_with("def")

    def test_uncovered_action_runs_unlocked(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        locker = with_locking(cmd, actions=["apply"])
        run = mocker.patch.object(cmd, "_run", return_value="result")
        cmd._run_locked("def", TerraformAction.PLAN)
        locker.acquire.assert_not_called()
        run.assert_called_once_with("def", TerraformAction.PLAN)


class TestLockUnavailable:
    def _error_results(self, cmd):
        return [
            c.kwargs["result"]
            for c in cmd.app_state.handlers.exec_handlers.call_args_list
            if c.kwargs.get("stage") == TerraformStage.ERROR
        ]

    def _locked(self, cmd, error):
        locker = with_locking(cmd)
        locker.acquire.side_effect = error
        return locker

    def test_plan_lock_fails_definition_plan(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        self._locked(cmd, LockUnavailableError("still locked"))
        run = mocker.patch.object(cmd, "_run")
        hook = mocker.patch.object(cmd, "_exec_hook")
        phases = mocker.patch.object(cmd, "_phases", create=True)
        cmd._exec_terraform_plan("def")
        definition = cmd.app_state.definitions["def"]
        assert phases.phase.call_args.kwargs["lock_wait"] is not None
        assert "def" not in cmd.lock_waits
        assert definition.plan_failed is True
        assert definition.needs_apply is False
        run.assert_not_called()
        hook.assert_not_called()
        cmd.ctx.exit.assert_not_called()
        (result,) = self._error_results(cmd)
        assert result.exit_code == 1
        assert result.stderr == b"still locked"
        assert result.started_at is not None

    def test_apply_lock_records_failure_and_exits(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        self._locked(cmd, LockUnavailableError("still locked"))
        mocker.patch.object(cmd, "_exec_hook", return_value=True)
        run = mocker.patch.object(cmd, "_run")
        with pytest.raises(SystemExit):
            cmd._exec_terraform_action("def", TerraformAction.APPLY)
        cmd.ctx.exit.assert_called_once_with(1)
        run.assert_not_called()
        assert cmd.apply_errors["def"] == "still locked"
        assert cmd.app_state.definitions["def"].apply_failed is True

    def test_fail_run_exits_from_plan(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        self._locked(cmd, LockUnavailableError("still locked", fail_run=True))
        with pytest.raises(SystemExit):
            cmd._exec_terraform_plan("def")
        cmd.ctx.exit.assert_called_once_with(1)

    def test_apply_lock_without_exit(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        self._locked(cmd, LockUnavailableError("still locked"))
        mocker.patch.object(cmd, "_exec_hook", return_value=True)
        result = cmd._exec_terraform_action(
            "def", TerraformAction.DESTROY, exit_on_error=False
        )
        assert result.exit_code == 1
        cmd.ctx.exit.assert_not_called()
        assert len(self._error_results(cmd)) == 1

    LOCK_STDERR = b"Error: Error acquiring the state lock\n\nLock Info: ..."

    @pytest.mark.parametrize("fail_run", [True, False])
    def test_terraform_lock_error_on_apply(self, tmp_path, mocker, fail_run):
        cmd = make_command(tmp_path)
        with_locking(cmd, on_timeout="fail_run" if fail_run else "fail_definition")
        mocker.patch.object(cmd, "_exec_hook", return_value=True)
        mocker.patch.object(
            cmd, "_run", return_value=TerraformResult(1, b"", self.LOCK_STDERR)
        )
        if fail_run:
            with pytest.raises(SystemExit):
                cmd._exec_terraform_action(
                    "def", TerraformAction.APPLY, exit_on_error=False
                )
        else:
            cmd._exec_terraform_action(
                "def", TerraformAction.APPLY, exit_on_error=False
            )
            cmd.ctx.exit.assert_not_called()

    def test_terraform_lock_error_on_plan_fails_run(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        with_locking(cmd, on_timeout="fail_run")
        definition = cmd.app_state.definitions["def"]
        definition.plan_file = tmp_path / "def.tfplan"
        mocker.patch.object(
            cmd, "_run", return_value=TerraformResult(1, b"", self.LOCK_STDERR)
        )
        with pytest.raises(SystemExit):
            cmd._exec_terraform_plan("def")
        assert definition.plan_failed is True

    def test_other_failures_are_not_lock_errors(self, tmp_path):
        cmd = make_command(tmp_path)
        with_locking(cmd, on_timeout="fail_run")
        assert not cmd._lock_held_failure("def", TerraformResult(1, b"", b"boom"))


class TestPhaseDispatch:
    """Core components hear about each phase alongside the handlers."""

    def _calls(self, phases):
        return [(c.args[0], c.args[1]) for c in phases.phase.call_args_list]

    def test_off_without_reporting(self, tmp_path):
        cmd = make_command(tmp_path)
        assert cmd.phases.listeners == []
        assert cmd.phases is cmd.phases

    def test_recorder_when_reporting_is_configured(self, tmp_path):
        from tfworker.custom_types.config_file import ReportingOptions
        from tfworker.reporting import RunRecorder

        cmd = make_command(tmp_path)
        cmd.app_state.loaded_config.reporting = ReportingOptions()
        cmd.app_state.backend = SimpleNamespace(report_storage=True)
        (listener,) = cmd.phases.listeners
        assert isinstance(listener, RunRecorder)

    def test_action_pre_and_post(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        phases = mocker.patch.object(cmd, "_phases", create=True)
        mocker.patch.object(cmd, "_exec_hook", return_value=True)
        result = TerraformResult(0, b"", b"")

        def run_locked(name, action):
            cmd.lock_waits[name] = 3.0
            return result

        mocker.patch.object(cmd, "_run_locked", side_effect=run_locked)
        cmd._exec_terraform_action("def", TerraformAction.APPLY)
        assert self._calls(phases) == [
            (TerraformAction.APPLY, TerraformStage.PRE),
            (TerraformAction.APPLY, TerraformStage.POST),
        ]
        pre, post = phases.phase.call_args_list
        assert pre.kwargs["lock_wait"] is None
        assert post.kwargs == {"result": result, "lock_wait": 3.0}
        assert "def" not in cmd.lock_waits

    def test_action_failure_is_error(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        phases = mocker.patch.object(cmd, "_phases", create=True)
        mocker.patch.object(cmd, "_exec_hook", return_value=True)
        mocker.patch.object(
            cmd, "_run_locked", return_value=TerraformResult(1, b"", b"boom")
        )
        cmd._exec_terraform_action("def", TerraformAction.INIT, exit_on_error=False)
        assert self._calls(phases)[-1] == (TerraformAction.INIT, TerraformStage.ERROR)

    def test_plan_pre_and_post(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        phases = mocker.patch.object(cmd, "_phases", create=True)
        mocker.patch.object(cmd, "_exec_hook", return_value=True)
        mocker.patch.object(cmd, "_generate_plan_output_json")
        mocker.patch.object(
            cmd, "_run_locked", return_value=TerraformResult(2, b"", b"")
        )
        assert cmd._exec_terraform_pre_plan("def")
        cmd._exec_terraform_plan("def")
        assert self._calls(phases) == [
            (TerraformAction.PLAN, TerraformStage.PRE),
            (TerraformAction.PLAN, TerraformStage.POST),
        ]

    def test_skip(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        phases = mocker.patch.object(cmd, "_phases", create=True)
        cmd._notify_skip("def", TerraformAction.PLAN, "no changes")
        cmd._notify_skip("def", TerraformAction.PLAN, "no changes")
        phases.skip.assert_called_once_with(
            TerraformAction.PLAN, cmd.app_state.definitions["def"], "no changes"
        )

    def test_post_handler_error_still_records_success(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        phases = mocker.patch.object(cmd, "_phases", create=True)
        mocker.patch.object(cmd, "_exec_hook", return_value=True)
        mocker.patch.object(
            cmd, "_run_locked", return_value=TerraformResult(0, b"", b"")
        )

        def fail_post(**kw):
            if kw["stage"] == TerraformStage.POST:
                raise HandlerError("slack down")

        cmd.app_state.handlers.exec_handlers.side_effect = fail_post
        with pytest.raises(SystemExit):
            cmd._exec_terraform_action("def", TerraformAction.APPLY)
        assert self._calls(phases)[-1] == (TerraformAction.APPLY, TerraformStage.POST)
        cmd.ctx.exit.assert_called_once_with(2)

    def test_post_plan_handler_error_still_records_success(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        phases = mocker.patch.object(cmd, "_phases", create=True)
        mocker.patch.object(cmd, "_exec_hook", return_value=True)
        mocker.patch.object(cmd, "_generate_plan_output_json")
        mocker.patch.object(
            cmd, "_run_locked", return_value=TerraformResult(2, b"", b"")
        )
        cmd.app_state.handlers.exec_handlers.side_effect = HandlerError("down")
        with pytest.raises(SystemExit):
            cmd._exec_terraform_plan("def")
        assert self._calls(phases) == [(TerraformAction.PLAN, TerraformStage.POST)]

    def test_pre_drops_a_stale_lock_wait(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        phases = mocker.patch.object(cmd, "_phases", create=True)
        cmd.lock_waits["def"] = 9.0
        cmd._dispatch_phase("def", TerraformAction.APPLY, TerraformStage.PRE)
        assert phases.phase.call_args.kwargs["lock_wait"] is None
        assert "def" not in cmd.lock_waits

    def test_order_is_handlers_then_core_then_hooks(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        order = []
        phases = mocker.patch.object(cmd, "_phases", create=True)
        phases.phase.side_effect = lambda a, s, *r, **k: order.append(("core", s))
        cmd.app_state.handlers.exec_handlers.side_effect = lambda **k: order.append(
            ("handlers", k["stage"])
        )

        def hook(definition, action, stage, result=None):
            order.append(("hooks", stage))
            return True

        mocker.patch.object(cmd, "_exec_hook", side_effect=hook)
        mocker.patch.object(
            cmd, "_run_locked", return_value=TerraformResult(0, b"", b"")
        )
        cmd._exec_terraform_action("def", TerraformAction.APPLY)
        pre, post = TerraformStage.PRE, TerraformStage.POST
        assert order == [
            ("handlers", pre),
            ("core", pre),
            ("hooks", pre),
            ("handlers", post),
            ("core", post),
            ("hooks", post),
        ]

    @pytest.mark.parametrize("stage", [TerraformStage.PRE, TerraformStage.POST])
    def test_hook_failure_is_core_error(self, tmp_path, mocker, stage):
        cmd = make_command(tmp_path)
        phases = mocker.patch.object(cmd, "_phases", create=True)
        mocker.patch.object(
            cmd, "_exec_hook", side_effect=lambda d, a, s, r=None: s != stage
        )
        mocker.patch.object(
            cmd, "_run_locked", return_value=TerraformResult(0, b"", b"")
        )
        cmd._exec_terraform_action("def", TerraformAction.APPLY, exit_on_error=False)
        action, last = self._calls(phases)[-1]
        assert (action, last) == (TerraformAction.APPLY, TerraformStage.ERROR)
        assert phases.phase.call_args.kwargs["result"].exit_code == 2

    def test_lock_unavailable_is_core_error_with_wait(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        phases = mocker.patch.object(cmd, "_phases", create=True)
        mocker.patch.object(cmd, "_exec_hook", return_value=True)
        locker = with_locking(cmd)
        locker.acquire.side_effect = LockUnavailableError("held")
        cmd._exec_terraform_action("def", TerraformAction.APPLY, exit_on_error=False)
        assert self._calls(phases)[-1] == (TerraformAction.APPLY, TerraformStage.ERROR)
        assert phases.phase.call_args.kwargs["lock_wait"] is not None

    def test_init_failure_is_core_error(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        phases = mocker.patch.object(cmd, "_phases", create=True)
        cmd._record_init_failure("def", "prepare failed")
        assert self._calls(phases) == [(TerraformAction.INIT, TerraformStage.ERROR)]

    def test_core_error_survives_a_failing_error_handler(self, tmp_path, mocker):
        cmd = make_command(tmp_path)
        phases = mocker.patch.object(cmd, "_phases", create=True)
        cmd.app_state.handlers.exec_handlers.side_effect = RuntimeError("boom")
        cmd._exec_error_handlers(
            "def", TerraformAction.PLAN, TerraformResult(1, b"", b"")
        )
        assert self._calls(phases) == [(TerraformAction.PLAN, TerraformStage.ERROR)]
