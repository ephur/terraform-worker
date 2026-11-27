from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from tfworker.cli_options import CLIOptionsRoot, CLIOptionsTerraform
from tfworker.commands import config as c
from tfworker.custom_types.config_file import ConfigFile

# captured before conftest's autouse fixture replaces it with a mock context
REAL_GET_CURRENT_CONTEXT = click.get_current_context


class TestLoadConfig:
    def test_single_file(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            """terraform:\n  worker_options:\n    foo: bar\n  definitions:\n    a:
      path: /a\n"""
        )
        loaded = c.load_config(str(cfg), {}, "d", str(tmp_path))
        assert isinstance(loaded, ConfigFile)
        assert "a" in loaded.definitions

    def test_merge_files(self, tmp_path):
        cfg1 = tmp_path / "cfg1.yaml"
        cfg1.write_text(
            """terraform:\n  worker_options:\n    a: one\n  definitions:\n    mod:
      path: /old\n"""
        )
        cfg2 = tmp_path / "cfg2.yaml"
        cfg2.write_text(
            """terraform:\n  worker_options:\n    b: two\n  definitions:\n    mod:\n      path: /new\n    extra:\n      path: /x\n"""
        )
        loaded = c.load_config([str(cfg1), str(cfg2)], {}, "d", str(tmp_path))
        assert loaded.worker_options["a"] == "one"
        assert loaded.worker_options["b"] == "two"
        assert loaded.definitions["mod"]["path"] == "/new"
        assert "extra" in loaded.definitions

    def test_parallel_options_defaults(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("""terraform:\n  definitions:\n    a:\n      path: /a\n""")
        loaded = c.load_config(str(cfg), {}, "d", str(tmp_path))
        assert loaded.parallel_options.max_preparation_workers == 8
        assert loaded.parallel_options.max_init_workers == 4

    def test_parallel_options_custom(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            """terraform:\n  parallel_options:\n    max_preparation_workers: 2\n    max_init_workers: 1\n  definitions:\n    a:\n      path: /a\n"""
        )
        loaded = c.load_config(str(cfg), {}, "d", str(tmp_path))
        assert loaded.parallel_options.max_preparation_workers == 2
        assert loaded.parallel_options.max_init_workers == 1

    def test_deployment_template_var(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            """terraform:\n  definitions:\n    a:\n      path: /{{ deployment }}\n"""
        )
        loaded = c.load_config(
            str(cfg), {"deployment": "my-dep"}, "my-dep", str(tmp_path)
        )
        assert loaded.definitions["a"]["path"] == "/my-dep"


class TestLoadConfigDefinitionsSources:
    def _write(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "terraform:\n"
            "  definitions_sources:\n"
            "    local:\n"
            "      path: catalog\n"
            "      command: bin/list-definitions\n"
            "      after: base\n"
            "  definitions:\n"
            "    base:\n"
            "      path: ./base\n"
            "    last:\n"
            "      path: ./last\n"
        )
        return cfg

    def test_generated_definitions_loaded(self, tmp_path, definitions_source):
        cfg = self._write(tmp_path)
        loaded = c.load_config(str(cfg), {}, "dep-one", str(tmp_path))
        assert list(loaded.definitions) == ["base", "generated", "last"]
        generated = loaded.definitions["generated"]
        assert generated["template_vars"]["deployment"] == "dep-one"
        assert generated["path"] == "catalog"
        assert loaded.definitions_sources["local"].command == "bin/list-definitions"

    def test_repository_path_config_var_ignored(self, tmp_path, definitions_source):
        cfg = self._write(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        loaded = c.load_config(
            str(cfg),
            {"repository_path": str(elsewhere)},
            "d",
            str(tmp_path),
        )
        assert "generated" in loaded.definitions

    def test_limit_accepts_generated_definition(
        self, tmp_path, definitions_source, mock_click_context
    ):
        cfg = self._write(tmp_path)
        loaded = c.load_config(str(cfg), {}, "d", str(tmp_path))
        mock_click_context.obj.loaded_config = loaded
        opts = CLIOptionsTerraform(limit=["generated"])
        assert opts.limit == ["generated"]

    def test_without_sources_nothing_runs(self, tmp_path, mocker):
        apply = mocker.patch("tfworker.commands.config.apply_definitions_sources")
        cfg = tmp_path / "config.yaml"
        cfg.write_text("terraform:\n  definitions:\n    a:\n      path: /a\n")
        loaded = c.load_config(str(cfg), {}, "d", str(tmp_path))
        apply.assert_not_called()
        assert loaded.definitions_sources == {}

    def test_source_failure_exits(
        self, tmp_path, mocker, mock_click_context, definitions_source
    ):
        cfg = self._write(tmp_path)
        (definitions_source / "bin" / "list-definitions").write_text(
            "#!/bin/sh\necho boom >&2\nexit 2\n"
        )
        error = mocker.patch("tfworker.util.log.error")
        with pytest.raises(SystemExit):
            c.load_config(str(cfg), {}, "d", str(tmp_path))
        mock_click_context.exit.assert_called_once_with(1)
        message = error.call_args.args[0]
        assert "local" in message
        assert "exited 2" in message
        assert "boom" in message

    def test_invalid_source_block_exits(self, tmp_path, mocker, mock_click_context):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "terraform:\n"
            "  definitions_sources:\n"
            "    local:\n"
            "      path: catalog\n"
            "      command: ./list\n"
            "      bogus: true\n"
        )
        error = mocker.patch("tfworker.util.log.error")
        with pytest.raises(SystemExit):
            c.load_config(str(cfg), {}, "d", str(tmp_path))
        mock_click_context.exit.assert_called_once_with(1)
        message = error.call_args.args[0]
        assert "definitions source named local" in message
        assert "bogus" in message


class TestProcessTemplate:
    def test_template_vars(self, tmp_path):
        tpl = tmp_path / "cfg.yaml"
        tpl.write_text("terraform:\n  worker_options:\n    name: {{ var.name }}\n")
        rendered = c._process_template(str(tpl), {"var": {"name": "test"}, "env": {}})
        assert "name: test" in rendered


class TestParameterSource:
    """Where a value came from decides whether the config file may replace it."""

    def _click_context(self, mocker, own: dict, parent: dict):
        """A subcommand context whose parent owns the group level options."""
        parent_ctx = mocker.MagicMock()
        parent_ctx.parent = None
        parent_ctx.get_parameter_source.side_effect = parent.get

        ctx = mocker.MagicMock()
        ctx.parent = parent_ctx
        ctx.get_parameter_source.side_effect = own.get

        mocker.patch("click.get_current_context", return_value=ctx)
        return ctx

    def test_source_from_the_current_context(self, mocker):
        self._click_context(
            mocker, own={"plan": click.core.ParameterSource.COMMANDLINE}, parent={}
        )
        assert c._parameter_source("plan") == click.core.ParameterSource.COMMANDLINE

    def test_source_from_a_parent_context(self, mocker):
        """A group level option is unknown to the subcommand that is running."""
        self._click_context(
            mocker,
            own={},
            parent={"aws_profile": click.core.ParameterSource.ENVIRONMENT},
        )
        assert (
            c._parameter_source("aws_profile") == click.core.ParameterSource.ENVIRONMENT
        )

    def test_unknown_parameter_has_no_source(self, mocker):
        self._click_context(mocker, own={}, parent={})
        assert c._parameter_source("nonsense") is None

    def test_no_context_has_no_source(self, mocker):
        mocker.patch("click.get_current_context", return_value=None)
        assert c._parameter_source("aws_profile") is None


class TestResolveModelWithCliOptions:
    """The config file fills in what the caller did not set, and nothing more."""

    def _run(self, env: dict, worker_options: dict, monkeypatch) -> str:
        """Invoke a group/subcommand pair the way the real CLI is shaped."""
        resolved = {}
        # the resolver reads the live context, which this test provides itself
        monkeypatch.setattr(click, "get_current_context", REAL_GET_CURRENT_CONTEXT)

        @click.group()
        @click.option("--aws-profile", envvar="AWS_PROFILE", default=None)
        @click.pass_context
        def cli(ctx, aws_profile):
            ctx.obj = SimpleNamespace(root_options=None)

        @cli.command()
        @click.pass_context
        def terraform(ctx):
            root = CLIOptionsRoot(aws_profile=ctx.parent.params["aws_profile"])
            app_state = SimpleNamespace(
                root_options=root,
                model_fields_set={"root_options"},
                loaded_config=SimpleNamespace(worker_options=dict(worker_options)),
            )
            c.resolve_model_with_cli_options(app_state, model_classes=[CLIOptionsRoot])
            resolved["aws_profile"] = root.aws_profile

        CliRunner().invoke(cli, ["terraform"], env=env, catch_exceptions=False)
        return resolved["aws_profile"]

    def test_environment_beats_the_config_file(self, monkeypatch):
        """A group level option set in the environment is not overwritten."""
        assert (
            self._run({"AWS_PROFILE": "working"}, {"aws_profile": "qa"}, monkeypatch)
            == "working"
        )

    def test_config_file_fills_in_an_unset_option(self, monkeypatch):
        assert self._run({}, {"aws_profile": "qa"}, monkeypatch) == "qa"
