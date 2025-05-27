import pytest

from tfworker import cli_options as c


class TestCLIOptionsRoot:
    """
    Tests covering base CLIOptionsRoot model and validation of its attributes
    """

    def test_cli_options_root_model(self):
        cli_options = c.CLIOptionsRoot()
        assert cli_options is not None

    def test_cli_options_with_invalid_backend(self):
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(backend="invalid_backend")

    def test_cli_options_with_valid_backend_str(self):
        from tfworker.backends import Backends

        cli_options = c.CLIOptionsRoot(backend="s3")
        assert cli_options.backend == Backends.S3

    def test_cli_options_with_valid_backend_enum(self):
        from tfworker.backends import Backends

        cli_options = c.CLIOptionsRoot(backend=Backends.S3)
        assert cli_options.backend == Backends.S3

    def test_cli_options_with_lower_log_level(self):
        cli_options = c.CLIOptionsRoot(log_level="debug")
        assert cli_options.log_level == "DEBUG"

    def test_cli_options_with_invalid_log_level(self):
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(log_level="invalid_log_level")

    def test_config_exists(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("config")
        cli_options = c.CLIOptionsRoot(config_file=str(config_file))
        assert cli_options.config_file == str(config_file)

    def test_config_does_not_exist(self):
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(config_file="config_file")

    def test_config_file_is_dir(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.mkdir()
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(config_file=str(config_file))

    def test_config_file_is_not_readable(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.touch()
        config_file.chmod(0o000)
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(config_file=str(config_file))
        config_file.chmod(0o644)

    def test_gcp_creds_path_exists(self, tmp_path):
        gcp_creds_path = tmp_path / "gcp_creds.json"
        gcp_creds_path.write_text("gcp_creds")
        cli_options = c.CLIOptionsRoot(gcp_creds_path=str(gcp_creds_path))
        assert cli_options.gcp_creds_path == str(gcp_creds_path)

    def test_gcp_creds_path_does_not_exist(self):
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(gcp_creds_path="gcp_creds_path")

    def test_gcp_creds_path_is_dir(self, tmp_path):
        gcp_creds_path = tmp_path / "gcp_creds.json"
        gcp_creds_path.mkdir()
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(gcp_creds_path=str(gcp_creds_path))

    def test_gcp_creds_path_is_none(self):
        cli_options = c.CLIOptionsRoot(gcp_creds_path=None)
        assert cli_options.gcp_creds_path is None

    def test_validate_backend_prefix_leading_slash(self):
        cli_options = c.CLIOptionsRoot(backend_prefix="/backend_prefix")
        assert cli_options.backend_prefix == "backend_prefix"

    def test_validate_backend_prefix_trailing_slash(self):
        cli_options = c.CLIOptionsRoot(backend_prefix="backend_prefix/")
        assert cli_options.backend_prefix == "backend_prefix"

    def test_validate_backend_prefix_leading_and_trailing_slash(self):
        cli_options = c.CLIOptionsRoot(backend_prefix="/backend_prefix/")
        assert cli_options.backend_prefix == "backend_prefix"

    def test_validate_backend_prefix_double_slashes(self):
        cli_options = c.CLIOptionsRoot(backend_prefix="backend//prefix")
        assert cli_options.backend_prefix == "backend/prefix"

    def test_repository_path_valid(self, tmp_path):
        repository_path = tmp_path
        cli_options = c.CLIOptionsRoot(repository_path=str(repository_path))
        assert cli_options.repository_path == str(repository_path)

    def test_repository_path_invalid(self):
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(repository_path="nonexistent_dir")

    def test_repository_path_is_file(self, tmp_path):
        repository_path = tmp_path / "file"
        repository_path.touch()
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(repository_path=str(repository_path))

    def test_repository_path_is_none(self):
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(repository_path=None)

    def test_repository_path_not_writable(self, tmp_path):
        repository_path = tmp_path / "dir"
        repository_path.mkdir()
        repository_path.chmod(0o444)
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(repository_path=str(repository_path))
        repository_path.chmod(0o755)

    def test_repository_path_not_readable(self, tmp_path):
        repository_path = tmp_path / "dir"
        repository_path.mkdir()
        repository_path.chmod(0o222)
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(repository_path=str(repository_path))
        repository_path.chmod(0o755)

    def test_working_dir_valid(self, tmp_path):
        working_dir = tmp_path
        cli_options = c.CLIOptionsRoot(working_dir=str(working_dir))
        assert cli_options.working_dir == str(working_dir)

    def test_working_dir_invalid(self):
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(working_dir="nonexistent_dir")

    def test_working_dir_is_file(self, tmp_path):
        working_dir = tmp_path / "file"
        working_dir.touch()
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(working_dir=str(working_dir))

    def test_working_dir_is_none(self):
        cli_options = c.CLIOptionsRoot(working_dir=None)
        assert cli_options.working_dir is None

    def test_working_dir_not_writable(self, tmp_path):
        working_dir = tmp_path / "dir"
        working_dir.mkdir()
        working_dir.chmod(0o444)
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(working_dir=str(working_dir))
        working_dir.chmod(0o755)

    def test_working_dir_not_readable(self, tmp_path):
        working_dir = tmp_path / "dir"
        working_dir.mkdir()
        working_dir.chmod(0o222)
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(working_dir=str(working_dir))
        working_dir.chmod(0o755)

    def test_working_dir_not_empty(self, tmp_path):
        working_dir = tmp_path / "dir"
        working_dir.mkdir()
        (working_dir / "file").touch()
        with pytest.raises(ValueError):
            c.CLIOptionsRoot(working_dir=str(working_dir))


class TestCLIOptionsTerraform:
    """
    Tests covering base CLIOptionsTerraform model and validation of its attributes
    """

    def test_cli_options_terraform_model(self):
        cli_options = c.CLIOptionsTerraform()
        assert cli_options is not None

    def test_validate_apply_and_destroy(self):
        with pytest.raises(ValueError):
            c.CLIOptionsTerraform(apply=True, destroy=True)

    def test_validate_terraform_bin(self, mocker, mock_click_context):
        mocker.patch("shutil.which", return_value="./terraform")
        mocker.patch("tfworker.cli_options.os.access", return_value=True)
        mocker.patch("tfworker.cli_options.os.path.isfile", return_value=True)
        mocker.patch("tfworker.cli_options.os.path.isabs", return_value=True)
        mocker.patch("tfworker.cli_options.os.path.exists", return_value=True)
        mocker.patch(
            "tfworker.cli_options.get_terraform_version", return_value="0.12.0"
        )
        mocker.patch("click.get_current_context", return_value=mock_click_context)
        cli_options = c.CLIOptionsTerraform(terraform_bin=None)
        assert cli_options.terraform_bin == "./terraform"

    def test_validate_terraform_bin_not_found(self):
        with pytest.raises(ValueError):
            c.CLIOptionsTerraform(terraform_bin="nonexistent_bin")

    def test_validate_terraform_bin_is_none(self, mocker):
        mocker.patch("shutil.which", return_value=None)
        with pytest.raises(ValueError):
            c.CLIOptionsTerraform(terraform_bin=None)

    def test_validate_terraform_bin_not_executable(self, tmp_path):
        terraform_bin = tmp_path / "terraform"
        terraform_bin.touch()
        terraform_bin.chmod(0o644)
        with pytest.raises(ValueError):
            c.CLIOptionsTerraform(terraform_bin=str(terraform_bin))
        terraform_bin.chmod(0o755)

    def test_validate_provider_cache_valid(self, tmp_path):
        provider_cache = tmp_path
        cli_options = c.CLIOptionsTerraform(provider_cache=str(provider_cache))
        assert cli_options.provider_cache == str(provider_cache)

    def test_validate_provider_cache_invalid(self):
        with pytest.raises(ValueError):
            c.CLIOptionsTerraform(provider_cache="nonexistent_dir")

    def test_validate_provider_cache_is_file(self, tmp_path):
        provider_cache = tmp_path / "file"
        provider_cache.touch()
        with pytest.raises(ValueError):
            c.CLIOptionsTerraform(provider_cache=str(provider_cache))

    def test_validate_provider_cache_is_none(self):
        cli_options = c.CLIOptionsTerraform(provider_cache=None)
        assert cli_options.provider_cache is None

    def test_validate_provider_cache_not_writable(self, tmp_path):
        provider_cache = tmp_path / "dir"
        provider_cache.mkdir()
        provider_cache.chmod(0o444)
        with pytest.raises(ValueError):
            c.CLIOptionsTerraform(provider_cache=str(provider_cache))
        provider_cache.chmod(0o755)

    def test_validate_provider_cache_not_readable(self, tmp_path):
        provider_cache = tmp_path / "dir"
        provider_cache.mkdir()
        provider_cache.chmod(0o222)
        with pytest.raises(ValueError):
            c.CLIOptionsTerraform(provider_cache=str(provider_cache))
        provider_cache.chmod(0o755)

    def test_validate_plan_file_path_valid(self, tmp_path):
        plan_file_path = tmp_path
        cli_options = c.CLIOptionsTerraform(plan_file_path=str(plan_file_path))
        assert cli_options.plan_file_path == str(plan_file_path)

    def test_validate_plan_file_path_invalid(self):
        with pytest.raises(ValueError):
            c.CLIOptionsTerraform(plan_file_path="nonexistent_dir")

    def test_validate_plan_file_path_is_file(self, tmp_path):
        plan_file_path = tmp_path / "dir"
        plan_file_path.touch()
        with pytest.raises(ValueError):
            c.CLIOptionsTerraform(plan_file_path=str(plan_file_path))

    def test_validate_plan_file_path_is_none(self):
        cli_options = c.CLIOptionsTerraform(plan_file_path=None)
        assert cli_options.plan_file_path is None

    def test_validate_plan_file_path_not_writable(self, tmp_path):
        plan_file_path = tmp_path / "dir"
        plan_file_path.mkdir()
        plan_file_path.chmod(0o444)
        with pytest.raises(ValueError):
            c.CLIOptionsTerraform(plan_file_path=str(plan_file_path))
        plan_file_path.chmod(0o755)

    def test_validate_plan_file_path_not_readable(self, tmp_path):
        plan_file_path = tmp_path / "dir"
        plan_file_path.mkdir()
        plan_file_path.chmod(0o222)
        with pytest.raises(ValueError):
            c.CLIOptionsTerraform(plan_file_path=str(plan_file_path))
        plan_file_path.chmod(0o755)

    def test_validate_limit_none(self):
        cli_options = c.CLIOptionsTerraform(limit=None)
        assert cli_options.limit is None

    def test_validate_limit_list(self, mocker, mock_click_context):
        mocker.patch("click.get_current_context", return_value=mock_click_context)
        mock_click_context.obj.loaded_config.definitions = {
            "module1": {},
            "module2": {},
        }
        cli_options = c.CLIOptionsTerraform(limit=["module1", "module2"])
        assert cli_options.limit == ["module1", "module2"]

    def test_validate_limit_csv(self, mocker, mock_click_context):
        mocker.patch("click.get_current_context", return_value=mock_click_context)
        mock_click_context.obj.loaded_config.definitions = {
            "module1": {},
            "module2": {},
        }
        cli_options = c.CLIOptionsTerraform(limit="module1,module2")
        assert cli_options.limit == ["module1", "module2"]

    def test_validate_limit_csv_in_list(self, mocker, mock_click_context):
        mocker.patch("click.get_current_context", return_value=mock_click_context)
        mock_click_context.obj.loaded_config.definitions = {
            "module1": {},
            "module2": {},
            "module3": {},
        }
        cli_options = c.CLIOptionsTerraform(limit=["module1,module2", "module3"])
        assert sorted(cli_options.limit) == sorted(["module1", "module2", "module3"])

    def test_validate_limit_not_in_config(self, mocker, mock_click_context):
        mocker.patch("click.get_current_context", return_value=mock_click_context)
        mock_click_context.obj.loaded_config.definitions = {"module1": {}}
        with pytest.raises(ValueError):
            c.CLIOptionsTerraform(limit=["module1", "module2"])


class TestCLIOptionsClean:
    """
    Tests covering base CLIOptionsClean model and validation of its attributes
    """

    def test_cli_options_clean_model(self):
        cli_options = c.CLIOptionsClean()
        assert cli_options is not None

    def test_validate_limit_none(self):
        cli_options = c.CLIOptionsClean(limit=None)
        assert cli_options.limit is None

    def test_validate_limit_list(self, mocker, mock_click_context):
        mocker.patch("click.get_current_context", return_value=mock_click_context)
        mock_click_context.obj.loaded_config.definitions = {
            "module1": {},
            "module2": {},
        }
        cli_options = c.CLIOptionsClean(limit=["module1", "module2"])
        assert cli_options.limit == ["module1", "module2"]

    def test_validate_limit_csv(self, mocker, mock_click_context):
        mocker.patch("click.get_current_context", return_value=mock_click_context)
        mock_click_context.obj.loaded_config.definitions = {
            "module1": {},
            "module2": {},
        }
        cli_options = c.CLIOptionsClean(limit="module1,module2")
        assert cli_options.limit == ["module1", "module2"]

    def test_validate_limit_csv_in_list(self, mocker, mock_click_context):
        mocker.patch("click.get_current_context", return_value=mock_click_context)
        mock_click_context.obj.loaded_config.definitions = {
            "module1": {},
            "module2": {},
            "module3": {},
        }
        cli_options = c.CLIOptionsClean(limit=["module1,module2", "module3"])
        assert sorted(cli_options.limit) == sorted(["module1", "module2", "module3"])

    def test_validate_limit_not_in_config(self, mocker, mock_click_context):
        mocker.patch("click.get_current_context", return_value=mock_click_context)
        mock_click_context.obj.loaded_config.definitions = {"module1": {}}
        with pytest.raises(ValueError):
            c.CLIOptionsClean(limit=["module1", "module2"])
