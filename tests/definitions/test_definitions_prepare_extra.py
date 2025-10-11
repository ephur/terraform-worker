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
    mocker.patch.object(Definition, "get_remote_vars", return_value={"foo": "bar"})
    def_prepare.create_local_vars("def1")
    outfile = (
        Path(definition.get_target_path(def_prepare._app_state.working_dir))
        / WORKER_LOCALS_FILENAME
    )
    assert (
        outfile.read_text()
        == "locals {\n  foo = data.terraform_remote_state.bar\n}\n\n"
    )


def test_create_worker_tf_calls_helpers(mocker, def_prepare):
    m1 = mocker.patch.object(def_prepare, "_get_remotes", return_value=["r"])
    m2 = mocker.patch.object(def_prepare, "_get_used_providers", return_value=["p"])
    m3 = mocker.patch.object(def_prepare, "_get_provider_content", return_value="PCONT")
    m4 = mocker.patch.object(def_prepare, "_write_worker_tf")
    def_prepare.create_worker_tf("def1")
    m1.assert_called_once_with("def1")
    m2.assert_called_once_with("def1")
    m3.assert_called_once_with("def1", ["p"])
    m4.assert_called_once_with("def1", ["r"], "PCONT", ["p"])


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


def test_get_provider_content(def_prepare):
    def_prepare._app_state.providers.required_hcl.return_value = "REQ"
    assert def_prepare._get_provider_content("def1", None) == "REQ"
    def_prepare._app_state.providers.required_hcl.assert_called_once_with(None)

    assert def_prepare._get_provider_content("def1", ["p"]) == ""


def test_get_used_providers_caches(def_prepare, mocker):
    mock_method = mocker.patch.object(
        Definition, "get_used_providers", side_effect=[["p"], ["other"]]
    )
    assert def_prepare._get_used_providers("def1") == ["p"]
    assert def_prepare._get_used_providers("def1") == ["p"]
    assert mock_method.call_count == 1


def test_get_remotes(def_prepare, mocker, definition):
    definition.remote_vars = {"a": "one.var", "b": "two.var"}
    def_prepare._app_state.terraform_options.backend_use_all_remotes = False
    def_prepare._app_state.backend.remotes = ["x", "y"]

    mock_get_remote_vars = mocker.patch.object(
        Definition, "get_remote_vars", return_value={"a": "one.var", "b": "two.var"}
    )

    assert def_prepare._get_remotes("def1") == ["one", "two"]
    mock_get_remote_vars.assert_called_once_with(
        global_vars=def_prepare._app_state.loaded_config.global_vars.remote_vars
    )

    def_prepare._app_state.terraform_options.backend_use_all_remotes = True
    assert def_prepare._get_remotes("def1") == ["x", "y"]


def test_get_remotes_with_global_inheritance(def_prepare, mocker, definition):
    definition.remote_vars = {"local_var": "local.state"}
    def_prepare._app_state.terraform_options.backend_use_all_remotes = False

    mock_get_remote_vars = mocker.patch.object(
        Definition,
        "get_remote_vars",
        return_value={"local_var": "local.state", "global_var": "global.state"},
    )

    result = def_prepare._get_remotes("def1")
    assert result == ["local", "global"]
    mock_get_remote_vars.assert_called_once_with(
        global_vars=def_prepare._app_state.loaded_config.global_vars.remote_vars
    )


def test_write_worker_tf(def_prepare, definition, mocker):
    def_prepare._app_state.providers.provider_hcl.return_value = "PROV"
    def_prepare._app_state.backend.hcl.return_value = "BACKEND"
    def_prepare._app_state.backend.data_hcl.return_value = "DATA"
    def_prepare._write_worker_tf("def1", ["r"], "REQ", ["p1"])
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
