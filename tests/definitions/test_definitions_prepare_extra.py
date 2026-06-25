import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import jinja2
import pytest

from tfworker.constants import (
    RESERVED_FILES,
    TF_PROVIDER_DEFAULT_LOCKFILE,
    WORKER_LOCALS_FILENAME,
    WORKER_TF_FILENAME,
    WORKER_TFVARS_FILENAME,
)
from tfworker.definitions import Definition, DefinitionsCollection
from tfworker.definitions.prepare import (
    DefinitionPrepare,
    copy,
    filter_templates,
    get_coppier,
    get_jinja_env,
    vars_typer,
    write_template_file,
)


@pytest.fixture
def def_prepare(tmp_path, mock_app_state):
    DefinitionsCollection.reset()
    defs = DefinitionsCollection({"def1": {"path": str(tmp_path)}})
    mock_app_state.definitions = defs
    mock_app_state.root_options.repository_path = "."
    # ensure backend and providers exist for tests
    mock_app_state.backend = MagicMock()
    mock_app_state.providers = MagicMock()
    target_dir = Path(mock_app_state.working_dir) / "definitions" / "def1"
    target_dir.mkdir(parents=True, exist_ok=True)
    return DefinitionPrepare(mock_app_state)


@pytest.fixture
def definition(def_prepare):
    return def_prepare._app_state.definitions["def1"]


def test_get_coppier_calls_factory(mocker):
    mock_create = mocker.patch(
        "tfworker.definitions.prepare.CopyFactory.create", return_value="COP"
    )
    result = get_coppier("src", "root")
    assert result == "COP"
    mock_create.assert_called_once_with(
        "src", root_path="root", conflicts=RESERVED_FILES
    )


def test_copy_invokes_copier(mocker):
    copier = mocker.MagicMock()
    copy(copier=copier, destination="dest", options=None)
    copier.copy.assert_called_once_with(destination="dest")


def test_copy_raises_file_not_found(mocker):
    copier = mocker.MagicMock()
    copier.copy.side_effect = FileNotFoundError
    with pytest.raises(FileNotFoundError):
        copy(copier=copier, destination="dest", options={})


def test_get_jinja_env(tmp_path):
    env = get_jinja_env(str(tmp_path), {"foo": "bar"})
    assert env.globals["foo"] == "bar"
    assert isinstance(env.loader, jinja2.FileSystemLoader)
    assert str(tmp_path) in env.loader.searchpath


def test_write_template_file_success(tmp_path):
    tpl_dir = tmp_path
    tpl = tpl_dir / "test.tf.j2"
    tpl.write_text("value={{ var.name }}")
    env = get_jinja_env(str(tpl_dir), {"var": {"name": "foo"}})
    write_template_file(env, str(tpl_dir), "test.tf.j2")
    output = (tpl_dir / "test.tf").read_text()
    assert "value=foo" in output


def test_write_template_file_already_exists(tmp_path):
    tpl_dir = tmp_path
    tpl = tpl_dir / "dup.tf.j2"
    tpl.write_text("foo")
    (tpl_dir / "dup.tf").write_text("exists")
    env = get_jinja_env(str(tpl_dir), {})
    with pytest.raises(Exception):
        write_template_file(env, str(tpl_dir), "dup.tf.j2")


def test_write_template_file_render_error(tmp_path):
    tpl_dir = tmp_path
    tpl = tpl_dir / "bad.tf.j2"
    tpl.write_text("{{ undefined_var }}")
    env = get_jinja_env(str(tpl_dir), {})
    with pytest.raises(Exception):
        write_template_file(env, str(tpl_dir), "bad.tf.j2")


def test_filter_templates():
    assert filter_templates("main.tf.j2")
    assert not filter_templates("main.tf")


def test_vars_typer_simple_types():
    assert vars_typer(True) == "true"
    assert vars_typer(False) == "false"
    assert vars_typer("foo") == '"foo"'


def test_vars_typer_complex():
    assert vars_typer(["a", "b"]) == json.dumps(["a", "b"])
    nested = {"a": True, "b": ["c", False]}
    assert vars_typer(nested) == json.dumps({"a": "true", "b": ["c", "false"]})
    assert vars_typer(["c", False], inner=True) == ["c", "false"]


def test_create_local_vars(mocker, def_prepare, definition):
    mocker.patch.object(
        Definition, "get_remote_vars", return_value={"foo": "bar.outputs.baz"}
    )
    def_prepare.create_local_vars("def1")
    outfile = (
        Path(definition.get_target_path(def_prepare._app_state.working_dir))
        / WORKER_LOCALS_FILENAME
    )
    assert (
        outfile.read_text()
        == "locals {\n  foo = data.terraform_remote_state.bar.outputs.baz\n}\n\n"
    )


def test_create_worker_tf_calls_helpers(mocker, def_prepare):
    m1 = mocker.patch.object(def_prepare, "_get_remotes", return_value=["r"])
    m2 = mocker.patch.object(def_prepare, "_get_provider_content", return_value="PCONT")
    m3 = mocker.patch.object(def_prepare, "_write_worker_tf")
    def_prepare.create_worker_tf("def1")
    m1.assert_called_once_with("def1")
    m2.assert_called_once_with("def1")
    m3.assert_called_once_with("def1", ["r"], "PCONT")


def test_create_terraform_vars(mocker, def_prepare, definition):
    mocker.patch.object(Definition, "get_terraform_vars", return_value={"a": 1})
    def_prepare.create_terraform_vars("def1")
    outfile = (
        Path(definition.get_target_path(def_prepare._app_state.working_dir))
        / WORKER_TFVARS_FILENAME
    )
    assert outfile.read_text().strip() == 'a = "1"'


def test_create_terraform_lockfile_skip(def_prepare, mocker):
    def_prepare._app_state.providers = None
    def_prepare._app_state.terraform_options.provider_cache = "cache"
    m = mocker.patch("tfworker.definitions.prepare.generate_terraform_lockfile")
    def_prepare.create_terraform_lockfile("def1")
    m.assert_not_called()


def test_create_terraform_lockfile_none_result(def_prepare, mocker):
    def_prepare._app_state.providers = MagicMock()
    def_prepare._app_state.terraform_options.provider_cache = "cache"
    mocker.patch(
        "tfworker.definitions.prepare.generate_terraform_lockfile", return_value=None
    )
    outfile = (
        Path(
            def_prepare._app_state.definitions["def1"].get_target_path(
                def_prepare._app_state.working_dir
            )
        )
        / TF_PROVIDER_DEFAULT_LOCKFILE
    )
    def_prepare.create_terraform_lockfile("def1")
    assert not outfile.exists()


def test_create_terraform_lockfile_writes(def_prepare, mocker):
    def_prepare._app_state.providers = MagicMock()
    def_prepare._app_state.terraform_options.provider_cache = "cache"
    mocker.patch(
        "tfworker.definitions.prepare.generate_terraform_lockfile", return_value="LOCK"
    )
    outfile = (
        Path(
            def_prepare._app_state.definitions["def1"].get_target_path(
                def_prepare._app_state.working_dir
            )
        )
        / TF_PROVIDER_DEFAULT_LOCKFILE
    )
    def_prepare.create_terraform_lockfile("def1")
    assert outfile.read_text() == "LOCK"


def test_download_modules_success(mocker, def_prepare, definition):
    mocker.patch(
        "tfworker.definitions.prepare.pipe_exec", return_value=(0, b"out", b"err")
    )
    mocker.patch(
        "tfworker.commands.terraform.TerraformResult",
        return_value=SimpleNamespace(exit_code=0, stdout=b"out", stderr=b"err"),
    )
    def_prepare.download_modules("def1", stream_output=False)


def test_download_modules_failure(mocker, def_prepare, definition):
    mocker.patch(
        "tfworker.definitions.prepare.pipe_exec", return_value=(1, b"out", b"err")
    )
    mocker.patch(
        "tfworker.commands.terraform.TerraformResult",
        return_value=SimpleNamespace(exit_code=1, stdout=b"out", stderr=b"err"),
    )
    with pytest.raises(Exception):
        def_prepare.download_modules("def1")


def test_get_provider_content(def_prepare, mocker, definition):
    mocker.patch.object(Definition, "get_used_providers", return_value=None)
    def_prepare._app_state.providers.required_hcl.return_value = "REQ"
    assert def_prepare._get_provider_content("def1") == "REQ"
    def_prepare._app_state.providers.required_hcl.assert_called_once_with(None)

    mocker.patch.object(Definition, "get_used_providers", return_value=["p"])
    assert def_prepare._get_provider_content("def1") == ""


def test_get_remotes(def_prepare, mocker, definition):
    definition.remote_vars = {"a": "one.outputs.var", "b": "two.outputs.var"}
    def_prepare._app_state.terraform_options.backend_use_all_remotes = False
    def_prepare._app_state.backend.remotes = ["x", "y"]

    mock_get_remote_vars = mocker.patch.object(
        Definition,
        "get_remote_vars",
        return_value={"a": "one.outputs.var", "b": "two.outputs.var"},
    )

    assert set(def_prepare._get_remotes("def1")) == {"one", "two"}
    mock_get_remote_vars.assert_called_once_with(
        global_vars=def_prepare._app_state.loaded_config.global_vars.remote_vars
    )

    def_prepare._app_state.terraform_options.backend_use_all_remotes = True
    assert def_prepare._get_remotes("def1") == ["x", "y"]


def test_get_remotes_with_global_inheritance(def_prepare, mocker, definition):
    definition.remote_vars = {"local_var": "local.outputs.state"}
    def_prepare._app_state.terraform_options.backend_use_all_remotes = False

    mock_get_remote_vars = mocker.patch.object(
        Definition,
        "get_remote_vars",
        return_value={
            "local_var": "local.outputs.state",
            "global_var": "global.outputs.state",
        },
    )

    result = def_prepare._get_remotes("def1")
    assert set(result) == {"local", "global"}
    mock_get_remote_vars.assert_called_once_with(
        global_vars=def_prepare._app_state.loaded_config.global_vars.remote_vars
    )


def test_write_worker_tf(def_prepare, definition, mocker):
    def_prepare._app_state.providers.provider_hcl.return_value = "PROV"
    def_prepare._app_state.backend.hcl.return_value = "BACKEND"
    def_prepare._app_state.backend.data_hcl.return_value = "DATA"
    mocker.patch.object(Definition, "get_used_providers", return_value=["p1"])
    def_prepare._write_worker_tf("def1", ["r"], "REQ")
    tf_path = (
        Path(definition.get_target_path(def_prepare._app_state.working_dir))
        / WORKER_TF_FILENAME
    )
    content = tf_path.read_text()
    assert "PROV" in content
    assert "BACKEND" in content
    assert "REQ" in content
    assert "DATA" in content


def test_get_template_vars(mocker, def_prepare, definition, monkeypatch):
    mocker.patch.object(Definition, "get_template_vars", return_value={"foo": "bar"})
    def_prepare._app_state.root_options.config_var = ["baz=qux"]
    monkeypatch.setenv("EXAMPLE", "value")
    result = def_prepare._get_template_vars("def1")
    assert result["var"]["foo"] == "bar"
    assert result["var"]["baz"] == "qux"
    assert result["env"]["EXAMPLE"] == "value"


# ===== Tests for nested remote_vars structures =====


class TestCreateLocalVarsNested:
    """Tests for create_local_vars with nested structures (dicts/lists)."""

    def test_create_local_vars_with_dict_entire_outputs(
        self, mocker, def_prepare, definition
    ):
        """Test creating locals with dict of entire outputs references."""
        remote_vars = {
            "vpcs": {
                "platform": "network1.outputs",
                "payments": "network2.outputs",
            }
        }
        mocker.patch.object(Definition, "get_remote_vars", return_value=remote_vars)
        def_prepare.create_local_vars("def1")

        outfile = (
            Path(definition.get_target_path(def_prepare._app_state.working_dir))
            / WORKER_LOCALS_FILENAME
        )
        content = outfile.read_text()

        # Check structure
        assert "locals {" in content
        assert "vpcs = {" in content
        assert '"platform" = data.terraform_remote_state.network1.outputs' in content
        assert '"payments" = data.terraform_remote_state.network2.outputs' in content

    def test_create_local_vars_with_dict_specific_keys(
        self, mocker, def_prepare, definition
    ):
        """Test creating locals with dict of specific output keys."""
        remote_vars = {
            "vpc_configs": {
                "platform": "network1.outputs.vpc_config",
                "payments": "network2.outputs.vpc_config",
            }
        }
        mocker.patch.object(Definition, "get_remote_vars", return_value=remote_vars)
        def_prepare.create_local_vars("def1")

        outfile = (
            Path(definition.get_target_path(def_prepare._app_state.working_dir))
            / WORKER_LOCALS_FILENAME
        )
        content = outfile.read_text()

        assert "vpc_configs = {" in content
        assert (
            '"platform" = data.terraform_remote_state.network1.outputs.vpc_config'
            in content
        )
        assert (
            '"payments" = data.terraform_remote_state.network2.outputs.vpc_config'
            in content
        )

    def test_create_local_vars_with_list(self, mocker, def_prepare, definition):
        """Test creating locals with list of references."""
        remote_vars = {
            "vpc_ids": [
                "network1.outputs.vpc_id",
                "network2.outputs.vpc_id",
                "network3.outputs.vpc_id",
            ]
        }
        mocker.patch.object(Definition, "get_remote_vars", return_value=remote_vars)
        def_prepare.create_local_vars("def1")

        outfile = (
            Path(definition.get_target_path(def_prepare._app_state.working_dir))
            / WORKER_LOCALS_FILENAME
        )
        content = outfile.read_text()

        assert "vpc_ids = [" in content
        assert "data.terraform_remote_state.network1.outputs.vpc_id," in content
        assert "data.terraform_remote_state.network2.outputs.vpc_id," in content
        assert "data.terraform_remote_state.network3.outputs.vpc_id," in content

    def test_create_local_vars_mixed_simple_and_complex(
        self, mocker, def_prepare, definition
    ):
        """Test creating locals with mix of simple strings and complex structures."""
        remote_vars = {
            # Simple (existing)
            "environment": "env_info.outputs.environment",
            # Dict
            "vpcs": {
                "platform": "network1.outputs",
                "payments": "network2.outputs",
            },
            # List
            "vpc_ids": [
                "network1.outputs.vpc_id",
                "network2.outputs.vpc_id",
            ],
        }
        mocker.patch.object(Definition, "get_remote_vars", return_value=remote_vars)
        def_prepare.create_local_vars("def1")

        outfile = (
            Path(definition.get_target_path(def_prepare._app_state.working_dir))
            / WORKER_LOCALS_FILENAME
        )
        content = outfile.read_text()

        # Simple string
        assert (
            "environment = data.terraform_remote_state.env_info.outputs.environment"
            in content
        )
        # Dict
        assert "vpcs = {" in content
        assert '"platform" = data.terraform_remote_state.network1.outputs' in content
        # List
        assert "vpc_ids = [" in content
        assert "data.terraform_remote_state.network1.outputs.vpc_id," in content

    def test_create_local_vars_nested_dict_in_dict(
        self, mocker, def_prepare, definition
    ):
        """Test creating locals with nested dict structures."""
        remote_vars = {
            "networks": {
                "production": {
                    "primary": "net1.outputs.vpc",
                    "secondary": "net2.outputs.vpc",
                },
                "staging": {
                    "primary": "net3.outputs.vpc",
                },
            }
        }
        mocker.patch.object(Definition, "get_remote_vars", return_value=remote_vars)
        def_prepare.create_local_vars("def1")

        outfile = (
            Path(definition.get_target_path(def_prepare._app_state.working_dir))
            / WORKER_LOCALS_FILENAME
        )
        content = outfile.read_text()

        assert "networks = {" in content
        assert '"production" = {' in content
        assert '"primary" = data.terraform_remote_state.net1.outputs.vpc' in content
        assert '"secondary" = data.terraform_remote_state.net2.outputs.vpc' in content
        assert '"staging" = {' in content
        assert '"primary" = data.terraform_remote_state.net3.outputs.vpc' in content

    def test_create_local_vars_empty_dict(self, mocker, def_prepare, definition):
        """Test creating locals with empty dict."""
        remote_vars = {"empty": {}}
        mocker.patch.object(Definition, "get_remote_vars", return_value=remote_vars)
        def_prepare.create_local_vars("def1")

        outfile = (
            Path(definition.get_target_path(def_prepare._app_state.working_dir))
            / WORKER_LOCALS_FILENAME
        )
        content = outfile.read_text()

        assert "empty = {}" in content

    def test_create_local_vars_empty_list(self, mocker, def_prepare, definition):
        """Test creating locals with empty list."""
        remote_vars = {"empty": []}
        mocker.patch.object(Definition, "get_remote_vars", return_value=remote_vars)
        def_prepare.create_local_vars("def1")

        outfile = (
            Path(definition.get_target_path(def_prepare._app_state.working_dir))
            / WORKER_LOCALS_FILENAME
        )
        content = outfile.read_text()

        assert "empty = []" in content


class TestGetRemotesNested:
    """Tests for _get_remotes with nested structures."""

    def test_get_remotes_from_dict(self, mocker, def_prepare, definition):
        """Test extracting remotes from dict structure."""
        remote_vars = {
            "vpcs": {
                "platform": "network1.outputs",
                "payments": "network2.outputs.vpc",
            }
        }
        mocker.patch.object(Definition, "get_remote_vars", return_value=remote_vars)
        def_prepare._app_state.terraform_options.backend_use_all_remotes = False

        remotes = def_prepare._get_remotes("def1")
        assert set(remotes) == {"network1", "network2"}

    def test_get_remotes_from_list(self, mocker, def_prepare, definition):
        """Test extracting remotes from list structure."""
        remote_vars = {
            "vpc_ids": [
                "net1.outputs.vpc_id",
                "net2.outputs.vpc_id",
                "net3.outputs.vpc_id",
            ]
        }
        mocker.patch.object(Definition, "get_remote_vars", return_value=remote_vars)
        def_prepare._app_state.terraform_options.backend_use_all_remotes = False

        remotes = def_prepare._get_remotes("def1")
        assert set(remotes) == {"net1", "net2", "net3"}

    def test_get_remotes_from_mixed_structures(self, mocker, def_prepare, definition):
        """Test extracting remotes from mixed simple and complex structures."""
        remote_vars = {
            "env": "env_info.outputs.environment",
            "vpcs": {
                "platform": "network1.outputs",
                "payments": "network2.outputs",
            },
            "db_endpoints": [
                "db1.outputs.endpoint",
                "db2.outputs.endpoint",
            ],
        }
        mocker.patch.object(Definition, "get_remote_vars", return_value=remote_vars)
        def_prepare._app_state.terraform_options.backend_use_all_remotes = False

        remotes = def_prepare._get_remotes("def1")
        assert set(remotes) == {"env_info", "network1", "network2", "db1", "db2"}

    def test_get_remotes_deduplicates(self, mocker, def_prepare, definition):
        """Test that duplicate state names are deduplicated."""
        remote_vars = {
            "vpc_id": "net1.outputs.vpc_id",
            "subnet_ids": "net1.outputs.subnet_ids",
            "cidr": "net1.outputs.cidr",
        }
        mocker.patch.object(Definition, "get_remote_vars", return_value=remote_vars)
        def_prepare._app_state.terraform_options.backend_use_all_remotes = False

        remotes = def_prepare._get_remotes("def1")
        assert remotes == ["net1"]

    def test_get_remotes_with_nested_dict(self, mocker, def_prepare, definition):
        """Test extracting remotes from deeply nested structures."""
        remote_vars = {
            "networks": {
                "production": {
                    "primary": "net1.outputs",
                    "secondary": "net2.outputs",
                },
                "staging": [
                    "net3.outputs.vpc_id",
                ],
            }
        }
        mocker.patch.object(Definition, "get_remote_vars", return_value=remote_vars)
        def_prepare._app_state.terraform_options.backend_use_all_remotes = False

        remotes = def_prepare._get_remotes("def1")
        assert set(remotes) == {"net1", "net2", "net3"}


class TestGenerateTfValue:
    """Tests for _generate_tf_value helper method."""

    def test_generate_simple_string(self, def_prepare):
        """Test generating HCL for simple string reference."""
        result = def_prepare._generate_tf_value("network1.outputs.vpc_id")
        assert result == "data.terraform_remote_state.network1.outputs.vpc_id"

    def test_generate_string_entire_outputs(self, def_prepare):
        """Test generating HCL for entire outputs reference."""
        result = def_prepare._generate_tf_value("network1.outputs")
        assert result == "data.terraform_remote_state.network1.outputs"

    def test_generate_dict_single_level(self, def_prepare):
        """Test generating HCL for single-level dict."""
        value = {
            "platform": "network1.outputs",
            "payments": "network2.outputs",
        }
        result = def_prepare._generate_tf_value(value, indent=1)

        assert "{" in result
        assert '"platform" = data.terraform_remote_state.network1.outputs' in result
        assert '"payments" = data.terraform_remote_state.network2.outputs' in result
        assert "}" in result

    def test_generate_list(self, def_prepare):
        """Test generating HCL for list."""
        value = [
            "net1.outputs.vpc_id",
            "net2.outputs.vpc_id",
        ]
        result = def_prepare._generate_tf_value(value, indent=1)

        assert "[" in result
        assert "data.terraform_remote_state.net1.outputs.vpc_id," in result
        assert "data.terraform_remote_state.net2.outputs.vpc_id," in result
        assert "]" in result

    def test_generate_empty_dict(self, def_prepare):
        """Test generating HCL for empty dict."""
        result = def_prepare._generate_tf_value({})
        assert result == "{}"

    def test_generate_empty_list(self, def_prepare):
        """Test generating HCL for empty list."""
        result = def_prepare._generate_tf_value([])
        assert result == "[]"

    def test_generate_nested_dict(self, def_prepare):
        """Test generating HCL for nested dict."""
        value = {
            "production": {
                "primary": "net1.outputs",
            }
        }
        result = def_prepare._generate_tf_value(value, indent=1)

        assert '"production" = {' in result
        assert '"primary" = data.terraform_remote_state.net1.outputs' in result

    def test_generate_invalid_type(self, def_prepare):
        """Test that invalid value type raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            def_prepare._generate_tf_value(123)  # Number not supported
        assert "Unsupported remote_vars value type" in str(exc_info.value)
