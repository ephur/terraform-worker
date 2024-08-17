from unittest import mock

import pytest

import tfworker.util.hooks as hooks
from tfworker.exceptions import HookError
from tfworker.types.terraform import TerraformAction, TerraformStage


# Fixture for a mock Terraform state file
@pytest.fixture
def mock_terraform_state():
    """A mock Terraform state file with a single remote state resource"""
    return """
    {
        "version": 4,
        "terraform_version": "0.13.5",
        "serial": 1,
        "lineage": "8a2b56d2-4e16-48de-9c5b-c640d6b3a52d",
        "outputs": {},
        "resources": [
            {
                "module": "module.remote_state",
                "mode": "data",
                "type": "terraform_remote_state",
                "name": "example",
                "provider": "provider[\"registry.terraform.io/hashicorp/terraform\"]",
                "instances": [
                    {
                        "schema_version": 0,
                        "attributes": {
                            "backend": "gcs",
                            "config": {},
                            "outputs": {
                                "key": "value",
                                "another_key": "another_value"
                            }
                        }
                    }
                ]
            }
        ]
    }
    """


@pytest.fixture
def mock_terraform_locals():
    """A mock Terraform locals file with two variables"""
    return """locals {
  local_key = data.terraform_remote_state.example.outputs.key
  local_another_key = data.terraform_remote_state.example.outputs.another_key
}
"""


# Test for `get_state_item`
class TestGetStateItem:
    @mock.patch("tfworker.util.hooks._get_state_item_from_output")
    @mock.patch("tfworker.util.hooks._get_state_item_from_remote")
    def test_get_state_item_from_output_success(self, mock_remote, mock_output):
        mock_output.return_value = '{"key": "value"}'
        result = hooks.get_state_item(
            "working_dir", {}, "terraform_bin", "state", "item"
        )
        assert result == '{"key": "value"}'
        mock_output.assert_called_once()
        mock_remote.assert_not_called()

    @mock.patch(
        "tfworker.util.hooks._get_state_item_from_output", side_effect=FileNotFoundError
    )
    @mock.patch("tfworker.util.hooks._get_state_item_from_remote")
    def test_get_state_item_from_remote_success(self, mock_remote, mock_output):
        mock_remote.return_value = '{"key": "value"}'
        result = hooks.get_state_item(
            "working_dir", {}, "terraform_bin", "state", "item"
        )
        assert result == '{"key": "value"}'
        mock_output.assert_called_once()
        mock_remote.assert_called_once()


# Test for `_get_state_item_from_output`
class TestGetStateItemFromOutput:
    @mock.patch("tfworker.util.hooks.pipe_exec")
    def test_get_state_item_from_output_success(self, mock_pipe_exec):
        mock_pipe_exec.return_value = (0, '{"key":"value"}', "")
        result = hooks._get_state_item_from_output(
            "working_dir", {}, "terraform_bin", "state", "item"
        )
        assert result == '{"key":"value"}'
        mock_pipe_exec.assert_called_once()

    @mock.patch("tfworker.util.hooks.pipe_exec", side_effect=FileNotFoundError)
    def test_get_state_item_from_output_file_not_found(self, mock_pipe_exec):
        with pytest.raises(FileNotFoundError):
            hooks._get_state_item_from_output(
                "working_dir", {}, "terraform_bin", "state", "item"
            )

    @mock.patch("tfworker.util.hooks.pipe_exec")
    def test_get_state_item_from_output_error(self, mock_pipe_exec):
        mock_pipe_exec.return_value = (1, "", "error".encode())
        with pytest.raises(HookError):
            hooks._get_state_item_from_output(
                "working_dir", {}, "terraform_bin", "state", "item"
            )

    @mock.patch("tfworker.util.hooks.pipe_exec")
    def test_get_state_item_from_output_empty_output(self, mock_pipe_exec):
        mock_pipe_exec.return_value = (0, None, "")
        with pytest.raises(HookError) as e:
            hooks._get_state_item_from_output(
                "working_dir", {}, "terraform_bin", "state", "item"
            )
        assert "Remote state item state.item is empty" in str(e.value)

    @mock.patch("tfworker.util.hooks.pipe_exec")
    def test_get_state_item_from_output_invalid_json(self, mock_pipe_exec):
        mock_pipe_exec.return_value = (0, "invalid_json", "")
        with pytest.raises(HookError) as e:
            hooks._get_state_item_from_output(
                "working_dir", {}, "terraform_bin", "state", "item"
            )
        assert "output is not in JSON format" in str(e.value)


# Test for `check_hooks`
class TestCheckHooks:
    @mock.patch("tfworker.util.hooks.os.path.isdir", return_value=True)
    @mock.patch(
        "tfworker.util.hooks.os.listdir",
        return_value=[f"{TerraformStage.PRE}_{TerraformAction.PLAN}"],
    )
    @mock.patch("tfworker.util.hooks.os.access", return_value=True)
    def test_check_hooks_exists(self, mock_access, mock_listdir, mock_isdir):
        result = hooks.check_hooks(
            TerraformStage.PRE, "working_dir", TerraformAction.PLAN
        )
        assert result is True
        mock_isdir.assert_called_once()
        mock_listdir.assert_called_once()
        mock_access.assert_called_once()

    @mock.patch("tfworker.util.hooks.os.path.isdir", return_value=False)
    def test_check_hooks_no_dir(self, mock_isdir):
        result = hooks.check_hooks("phase", "working_dir", "command")
        assert result is False
        mock_isdir.assert_called_once()

    @mock.patch("tfworker.util.hooks.os.path.isdir", return_value=True)
    @mock.patch(
        "tfworker.util.hooks.os.listdir",
        return_value=[f"{TerraformStage.PRE}_{TerraformAction.PLAN}"],
    )
    @mock.patch("tfworker.util.hooks.os.access", return_value=False)
    def test_check_hooks_not_executable(self, mock_listdir, mock_isdir, mock_access):
        with pytest.raises(HookError) as e:
            hooks.check_hooks(TerraformStage.PRE, "working_dir", TerraformAction.PLAN)
        assert "working_dir/hooks/pre_plan exists, but is not executable!" in str(
            e.value
        )

    @mock.patch("tfworker.util.hooks.os.path.isdir", return_value=True)
    @mock.patch("tfworker.util.hooks.os.listdir", return_value=[])
    def test_check_hooks_no_hooks(self, mock_listdir, mock_isdir):
        result = hooks.check_hooks("phase", "working_dir", "command")
        assert result is False


# Test for `hook_exec`
class TestHookExec:
    @mock.patch("tfworker.util.hooks._prepare_environment")
    @mock.patch("tfworker.util.hooks._find_hook_script")
    @mock.patch("tfworker.util.hooks._populate_environment_with_terraform_variables")
    @mock.patch("tfworker.util.hooks._populate_environment_with_terraform_remote_vars")
    @mock.patch("tfworker.util.hooks._populate_environment_with_extra_vars")
    @mock.patch("tfworker.util.hooks._execute_hook_script")
    def test_hook_exec_success(
        self,
        mock_execute,
        mock_extra_vars,
        mock_remote_vars,
        mock_terraform_vars,
        mock_find_script,
        mock_prepare_env,
    ):
        mock_find_script.return_value = "hook_script"
        hooks.hook_exec(
            "phase",
            "command",
            "working_dir",
            {},
            "terraform_path",
            debug=True,
            b64_encode=True,
        )
        mock_prepare_env.assert_called_once()
        mock_find_script.assert_called_once()
        mock_terraform_vars.assert_called_once()
        mock_remote_vars.assert_called_once()
        mock_extra_vars.assert_called_once()
        mock_execute.assert_called_once()


# Helper function tests
class TestHelperFunctions:
    @mock.patch(
        "tfworker.util.hooks.os.listdir",
        return_value=[f"{TerraformStage.PRE}_{TerraformAction.PLAN}"],
    )
    def test_find_hook_script(self, mock_listdir):
        result = hooks._find_hook_script(
            "working_dir", TerraformStage.PRE, TerraformAction.PLAN
        )
        assert result == "working_dir/hooks/pre_plan"

    @mock.patch("tfworker.util.hooks.os.listdir", return_value=[])
    def test_find_hook_script_no_dir(self, mock_listdir):
        with pytest.raises(HookError) as e:
            hooks._find_hook_script(
                "working_dir", TerraformStage.PRE, TerraformAction.PLAN
            )
        assert "Hook script missing from" in str(e.value)

    def test_prepare_environment(self):
        env = {"KEY": "value"}
        result = hooks._prepare_environment(env, "terraform_path")
        assert result["KEY"] == "value"
        assert result["TF_PATH"] == "terraform_path"

    def test_set_hook_env_var(self):
        local_env = {}
        hooks._set_hook_env_var(
            local_env, hooks.TFHookVarType.VAR, "key", "value", False
        )
        assert local_env["TF_VAR_KEY"] == "value"

    @mock.patch("tfworker.util.hooks.pipe_exec")
    def test_execute_hook_script(self, mock_pipe_exec, capsys):
        import tfworker.util.log as log

        old_log_level = log.log_level
        log.log_level = log.LogLevel.DEBUG
        mock_pipe_exec.return_value = (0, b"stdout", b"stderr")
        hooks._execute_hook_script(
            "hook_script",
            TerraformStage.PRE,
            TerraformAction.PLAN,
            "working_dir",
            {},
            True,
        )
        mock_pipe_exec.assert_called_once_with(
            "hook_script pre plan", cwd="working_dir/hooks", env={}, stream_output=False
        )
        captured = capsys.readouterr()
        captured_lines = captured.out.splitlines()
        log.log_level = old_log_level
        assert len(captured_lines) == 4
        assert "Results from hook script: hook_script" in captured_lines
        assert "exit code: 0" in captured_lines
        assert "stdout: stdout" in captured_lines
        assert "stderr: stderr" in captured_lines

    @mock.patch("tfworker.util.hooks.os.path.isfile", return_value=True)
    @mock.patch(
        "builtins.open",
        new_callable=mock.mock_open,
        read_data="key=value\nanother_key=another_value",
    )
    def test_populate_environment_with_terraform_variables(
        self, mock_isfile, mock_open
    ):
        local_env = {}
        hooks._populate_environment_with_terraform_variables(
            local_env, "working_dir", "terraform_path", False
        )
        assert "TF_VAR_KEY" in local_env
        assert "TF_VAR_ANOTHER_KEY" in local_env

    @mock.patch("builtins.open", new_callable=mock.mock_open)
    @mock.patch("tfworker.util.hooks.os.path.isfile", return_value=True)
    @mock.patch(
        "tfworker.util.hooks.get_state_item", side_effect=["value", "another_value"]
    )
    def test_populate_environment_with_terraform_remote_vars(
        self, mock_get_state_item, mock_isfile, mock_open, mock_terraform_locals
    ):
        mock_open.return_value.read.return_value = mock_terraform_locals

        local_env = {}
        hooks._populate_environment_with_terraform_remote_vars(
            local_env, "working_dir", "terraform_path", False
        )
        assert "TF_REMOTE_LOCAL_KEY" in local_env.keys()
        assert "TF_REMOTE_LOCAL_ANOTHER_KEY" in local_env.keys()
        assert local_env["TF_REMOTE_LOCAL_KEY"] == "value"
        assert local_env["TF_REMOTE_LOCAL_ANOTHER_KEY"] == "another_value"

    def test_populate_environment_with_extra_vars(self):
        local_env = {}
        extra_vars = {"extra_key": "extra_value"}
        hooks._populate_environment_with_extra_vars(local_env, extra_vars, False)
        assert "TF_EXTRA_EXTRA_KEY" in local_env
