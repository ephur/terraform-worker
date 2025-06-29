import types

import pytest
from click import globals as click_globals
from click.testing import CliRunner

import tfworker.cli as cli
from tfworker.cli_options import CLIOptionsClean, CLIOptionsRoot, CLIOptionsTerraform


class TestCliRoot:
    def test_validate_host_failure(self, mocker):
        runner = CliRunner()
        mocker.patch("click.get_current_context", click_globals.get_current_context)
        mocker.patch("tfworker.cli.register_plugins")
        mocker.patch("tfworker.cli.EnvCommand")
        mocker.patch("tfworker.cli.RootCommand")
        mocker.patch(
            "tfworker.cli.CLIOptionsRoot.model_validate",
            return_value=CLIOptionsRoot.model_construct(),
        )
        logmsg = mocker.patch("tfworker.cli.log.msg")
        mocker.patch(
            "tfworker.cli.validate_host", side_effect=NotImplementedError("bad")
        )

        result = runner.invoke(cli.cli, ["env", "dep"])
        assert result.exit_code == 1
        logmsg.assert_called_once_with("bad", cli.log.LogLevel.ERROR)

    def test_option_validation_error(self, mocker):
        runner = CliRunner()
        mocker.patch("click.get_current_context", click_globals.get_current_context)
        mocker.patch("tfworker.cli.register_plugins")
        mocker.patch("tfworker.cli.EnvCommand")
        mocker.patch("tfworker.cli.RootCommand")
        mocker.patch("tfworker.cli.validate_host")

        err = None
        try:
            CLIOptionsRoot.model_validate({"log_level": 1})
        except cli.ValidationError as e:  # produce a ValidationError
            err = e
        mocker.patch("tfworker.cli.CLIOptionsRoot.model_validate", side_effect=err)
        handle = mocker.patch(
            "tfworker.cli.handle_option_error", side_effect=SystemExit(1)
        )

        result = runner.invoke(cli.cli, ["env", "dep"])
        assert result.exit_code == 1
        handle.assert_called_once_with(err)


class TestCliCommands:
    def test_clean_runs(self, mocker):
        runner = CliRunner()
        mocker.patch("click.get_current_context", click_globals.get_current_context)
        mocker.patch("tfworker.cli.validate_host")
        mocker.patch("tfworker.cli.register_plugins")
        root = mocker.patch("tfworker.cli.RootCommand")
        clean_cls = mocker.patch("tfworker.cli.CleanCommand")
        mocker.patch(
            "tfworker.cli.CLIOptionsRoot.model_validate",
            return_value=CLIOptionsRoot.model_construct(),
        )
        mocker.patch(
            "tfworker.cli.CLIOptionsClean.model_validate",
            return_value=CLIOptionsClean.model_construct(),
        )
        mocker.patch("tfworker.cli.log_limiter")

        result = runner.invoke(cli.cli, ["clean", "dep"])
        assert result.exit_code == 0
        root.assert_called_once()
        clean_cls.assert_called_once_with(deployment="dep")
        clean_cls.return_value.exec.assert_called_once()

    def test_terraform_runs(self, mocker):
        runner = CliRunner()
        mocker.patch("click.get_current_context", click_globals.get_current_context)
        mocker.patch("tfworker.cli.validate_host")
        mocker.patch("tfworker.cli.register_plugins")
        root = mocker.patch("tfworker.cli.RootCommand")
        tfc_cls = mocker.patch("tfworker.cli.TerraformCommand")
        mocker.patch(
            "tfworker.cli.CLIOptionsRoot.model_validate",
            return_value=CLIOptionsRoot.model_construct(),
        )
        mocker.patch(
            "tfworker.cli.CLIOptionsTerraform.model_validate",
            return_value=CLIOptionsTerraform.model_construct(),
        )
        mocker.patch("tfworker.cli.tf_util.get_terraform_version", return_value=(1, 0))
        mocker.patch("tfworker.cli.log_limiter")

        result = runner.invoke(cli.cli, ["terraform", "dep"])
        assert result.exit_code == 0
        root.assert_called_once()
        tfc_cls.assert_called_once_with(deployment="dep")
        for meth in [
            "prep_providers",
            "terraform_init",
            "terraform_plan",
            "terraform_apply_or_destroy",
        ]:
            getattr(tfc_cls.return_value, meth).assert_called_once()

    def test_env_runs(self, mocker):
        runner = CliRunner()
        mocker.patch("click.get_current_context", click_globals.get_current_context)
        mocker.patch("tfworker.cli.validate_host")
        mocker.patch("tfworker.cli.register_plugins")
        root = mocker.patch("tfworker.cli.RootCommand")
        env_cls = mocker.patch("tfworker.cli.EnvCommand")
        mocker.patch(
            "tfworker.cli.CLIOptionsRoot.model_validate",
            return_value=CLIOptionsRoot.model_construct(),
        )

        result = runner.invoke(cli.cli, ["env", "dep"])
        assert result.exit_code == 0
        root.assert_called_once()
        env_cls.assert_called_once_with(deployment="dep")
        env_cls.return_value.exec.assert_called_once()


class TestRegisterPlugins:
    def test_register_plugins_imports(self, mocker):
        imported = []

        real_import = __import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name in ("tfworker.handlers", "tfworker.copier"):
                imported.append(name)
                return types.ModuleType(name)
            return real_import(name, globals, locals, fromlist, level)

        mocker.patch("builtins.__import__", side_effect=fake_import)
        trace = mocker.patch("tfworker.cli.log.trace")

        cli.register_plugins()

        assert "tfworker.handlers" in imported
        assert "tfworker.copier" in imported
        trace.assert_any_call("registering handlers")
        trace.assert_any_call("registering copiers")

    def test_clean_option_error(self, mocker):
        runner = CliRunner()
        mocker.patch("click.get_current_context", click_globals.get_current_context)
        mocker.patch("tfworker.cli.validate_host")
        mocker.patch("tfworker.cli.register_plugins")
        mocker.patch("tfworker.cli.RootCommand")
        mocker.patch("tfworker.cli.CleanCommand")
        mocker.patch(
            "tfworker.cli.CLIOptionsRoot.model_validate",
            return_value=CLIOptionsRoot.model_construct(),
        )
        from pydantic import BaseModel, ValidationError

        class Dummy(BaseModel):
            a: int

        try:
            Dummy(a="b")
        except ValidationError as e:
            err = e
        mocker.patch("tfworker.cli.CLIOptionsClean.model_validate", side_effect=err)
        handle = mocker.patch(
            "tfworker.cli.handle_option_error", side_effect=SystemExit(1)
        )

        result = runner.invoke(cli.cli, ["clean", "dep"])
        assert result.exit_code == 1
        handle.assert_called_once_with(err)

    def test_terraform_option_error(self, mocker):
        runner = CliRunner()
        mocker.patch("click.get_current_context", click_globals.get_current_context)
        mocker.patch("tfworker.cli.validate_host")
        mocker.patch("tfworker.cli.register_plugins")
        mocker.patch("tfworker.cli.RootCommand")
        mocker.patch("tfworker.cli.TerraformCommand")
        mocker.patch(
            "tfworker.cli.CLIOptionsRoot.model_validate",
            return_value=CLIOptionsRoot.model_construct(),
        )
        from pydantic import BaseModel, ValidationError

        class Dummy(BaseModel):
            a: int

        try:
            Dummy(a="b")
        except ValidationError as e:
            err = e
        mocker.patch("tfworker.cli.CLIOptionsTerraform.model_validate", side_effect=err)
        handle = mocker.patch(
            "tfworker.cli.handle_option_error", side_effect=SystemExit(1)
        )

        result = runner.invoke(cli.cli, ["terraform", "dep"])
        assert result.exit_code == 1
        handle.assert_called_once_with(err)


class TestModuleExecution:
    def test_module_main_executes_cli(self, mocker):
        import runpy
        import sys

        mocker.patch("tfworker.cli.validate_host")
        mocker.patch("tfworker.cli.register_plugins")
        mocker.patch("tfworker.cli.RootCommand")
        mocker.patch("tfworker.cli.EnvCommand")

        argv = sys.argv
        sys.argv = ["tfworker.cli", "--help"]
        try:
            with pytest.raises(SystemExit):
                runpy.run_module("tfworker.cli", run_name="__main__")
        finally:
            sys.argv = argv
