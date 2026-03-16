import base64
import shlex
from unittest import mock

import pytest

import tfworker.util.hooks as hooks
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.exceptions import HookError


@pytest.fixture
def mock_terraform_locals():
    """A mock Terraform locals file with two variables"""
    return """locals {
  local_key = data.terraform_remote_state.example.outputs.key
  local_another_key = data.terraform_remote_state.example.outputs.another_key
}
"""


# Test for `get_state_item` - simplified to use only backend.get_state()
class TestGetStateItem:
    def test_get_state_item_success(self):
        """Test successful retrieval of state item from backend"""
        mock_backend = mock.Mock()
        # Mock the actual state file format from backend.get_state()
        mock_backend.get_state.return_value = {
            "version": 4,
            "outputs": {"test_item": {"value": "test_value", "type": "string"}},
            "resources": [],
        }

        result = hooks.get_state_item(
            "working_dir", {}, "terraform_bin", "test_state", "test_item", mock_backend
        )

        # Should return JSON with the output value
        import json

        result_data = json.loads(result)
        assert result_data == {"value": "test_value", "type": "string"}
        mock_backend.get_state.assert_called_once_with("test_state")

    def test_get_state_item_with_cache(self):
        """Test that state caching prevents multiple backend calls"""
        mock_backend = mock.Mock()
        # Mock the actual state file format
        state_data = {
            "version": 4,
            "outputs": {
                "item1": {"value": "value1", "type": "string"},
                "item2": {"value": "value2", "type": "string"},
            },
            "resources": [],
        }
        mock_backend.get_state.return_value = state_data

        state_cache = {}

        # First call - should fetch from backend
        result1 = hooks.get_state_item(
            "working_dir",
            {},
            "terraform_bin",
            "cached_state",
            "item1",
            mock_backend,
            state_cache,
        )

        # Second call - should use cache
        result2 = hooks.get_state_item(
            "working_dir",
            {},
            "terraform_bin",
            "cached_state",
            "item2",
            mock_backend,
            state_cache,
        )

        # Backend should only be called once
        assert mock_backend.get_state.call_count == 1

        import json

        assert json.loads(result1) == {"value": "value1", "type": "string"}
        assert json.loads(result2) == {"value": "value2", "type": "string"}

    def test_get_state_item_backend_not_implemented(self):
        """Test error when backend doesn't support get_state"""
        mock_backend = mock.Mock()
        mock_backend.get_state.side_effect = NotImplementedError()

        with pytest.raises(HookError) as e:
            hooks.get_state_item(
                "working_dir", {}, "terraform_bin", "state", "item", mock_backend
            )
        assert "does not support get_state" in str(e.value)

    def test_get_state_item_no_outputs(self):
        """Test error when state has no outputs"""
        mock_backend = mock.Mock()
        mock_backend.get_state.return_value = {
            "version": 4,
            "outputs": {},  # Empty outputs
            "resources": [],
        }

        with pytest.raises(HookError) as e:
            hooks.get_state_item(
                "working_dir",
                {},
                "terraform_bin",
                "test_state",
                "item",
                mock_backend,
            )
        assert "No outputs found in state 'test_state'" in str(e.value)

    def test_get_state_item_output_not_found(self):
        """Test error when output item not found in state"""
        mock_backend = mock.Mock()
        mock_backend.get_state.return_value = {
            "version": 4,
            "outputs": {"some_other_item": {"value": "test", "type": "string"}},
            "resources": [],
        }

        with pytest.raises(HookError) as e:
            hooks.get_state_item(
                "working_dir",
                {},
                "terraform_bin",
                "test_state",
                "missing_item",
                mock_backend,
            )
        assert "Output 'missing_item' not found in state 'test_state'" in str(e.value)


# Test for `_parse_tfvars_file`
class TestParseTfvarsFile:
    @mock.patch(
        "builtins.open",
        new_callable=mock.mock_open,
        read_data='str_var = "hello"\nnum_var = 42\nbool_var = true\nlist_var = [1, 2, 3]\nmap_var = {key = "value"}',
    )
    @mock.patch("tfworker.util.hooks.hcl2.load")
    def test_parse_tfvars_file_with_various_types(self, mock_hcl2_load, mock_open):
        """Test that _parse_tfvars_file correctly parses various HCL2 types"""
        mock_hcl2_load.return_value = {
            "str_var": "hello",
            "num_var": 42,
            "bool_var": True,
            "list_var": [1, 2, 3],
            "map_var": {"key": "value"},
        }

        result = hooks._parse_tfvars_file("/path/to/test.tfvars")

        assert result["str_var"] == "hello"
        assert result["num_var"] == 42
        assert result["bool_var"] is True
        assert result["list_var"] == [1, 2, 3]
        assert result["map_var"] == {"key": "value"}
        mock_hcl2_load.assert_called_once()

    @mock.patch(
        "builtins.open",
        new_callable=mock.mock_open,
        read_data="key = value\nanother_key = another_value",
    )
    @mock.patch("tfworker.util.hooks.hcl2.load")
    @mock.patch("tfworker.util.hooks.log.warn")
    def test_parse_tfvars_file_fallback_on_parse_error(
        self, mock_log_warn, mock_hcl2_load, mock_open
    ):
        """Test that _parse_tfvars_file falls back to simple parsing on HCL2 error"""
        # Use a generic exception to test the fallback behavior
        mock_hcl2_load.side_effect = Exception("HCL2 parsing error")

        result = hooks._parse_tfvars_file("/path/to/test.tfvars")

        # Should fall back to simple parsing
        assert "key" in result
        assert "another_key" in result
        # Warning should be logged
        mock_log_warn.assert_called_once()
        assert "Failed to parse" in mock_log_warn.call_args[0][0]

    @mock.patch("builtins.open", side_effect=FileNotFoundError)
    def test_parse_tfvars_file_not_found(self, mock_open):
        """Test that FileNotFoundError is propagated"""
        with pytest.raises(FileNotFoundError):
            hooks._parse_tfvars_file("/path/to/missing.tfvars")

    @mock.patch("tfworker.util.hooks.os.path.isfile", return_value=True)
    @mock.patch("tfworker.util.hooks._parse_tfvars_file")
    def test_populate_environment_with_terraform_variables_boolean(
        self, mock_parse_tfvars, mock_isfile
    ):
        """Test that boolean values from .tfvars are correctly converted to TRUE/FALSE"""
        mock_parse_tfvars.return_value = {
            "enabled": True,
            "disabled": False,
            "string_var": "some_value",
        }

        local_env = {}
        hooks._populate_environment_with_terraform_variables(
            local_env, "working_dir", "terraform_path", False
        )

        # Booleans should be uppercase TRUE/FALSE (not b64 encoded)
        assert "TF_VAR_ENABLED" in local_env
        assert "TF_VAR_DISABLED" in local_env
        # Should extract the value from shlex.quote
        assert shlex.split(local_env["TF_VAR_ENABLED"])[0] == "TRUE"
        assert shlex.split(local_env["TF_VAR_DISABLED"])[0] == "FALSE"


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

    @mock.patch("tfworker.util.hooks._prepare_environment")
    @mock.patch("tfworker.util.hooks._find_hook_script")
    @mock.patch("tfworker.util.hooks._populate_environment_with_terraform_variables")
    @mock.patch("tfworker.util.hooks._populate_environment_with_terraform_remote_vars")
    @mock.patch("tfworker.util.hooks._populate_environment_with_extra_vars")
    @mock.patch("tfworker.util.hooks._execute_hook_script")
    def test_hook_exec_remotes_disabled(
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
            disable_remote_state_vars=True,
        )
        mock_prepare_env.assert_called_once()
        mock_find_script.assert_called_once()
        mock_terraform_vars.assert_called_once()
        mock_remote_vars.assert_not_called()
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

    @pytest.mark.parametrize("b64_encode", [False, True])
    @pytest.mark.parametrize(
        "value",
        [
            {"a": 1, "b": [2, 3]},
            [1, 2, 3],
        ],
    )
    def test_set_hook_env_var_complex_parametrized(self, value, b64_encode):
        local_env = {}
        hooks._set_hook_env_var(
            local_env, hooks.TFHookVarType.EXTRA, "complex", value, b64_encode
        )
        stored = local_env["TF_EXTRA_COMPLEX"]
        assert isinstance(stored, str)
        if b64_encode:
            decoded_json = base64.b64decode(stored).decode()
            assert value == __import__("json").loads(decoded_json)
        else:
            # recover original JSON string by shell splitting
            decoded = shlex.split(stored)[0]
            assert value == __import__("json").loads(decoded)

    @pytest.mark.parametrize("b64_encode", [False, True])
    def test_set_hook_env_var_simple_and_bool_parametrized(self, b64_encode):
        local_env = {}
        simple_value = "hello world"
        hooks._set_hook_env_var(
            local_env, hooks.TFHookVarType.VAR, "simple", simple_value, b64_encode
        )
        stored_simple = local_env["TF_VAR_SIMPLE"]
        assert isinstance(stored_simple, str)
        if b64_encode:
            assert base64.b64decode(stored_simple).decode() == simple_value
        else:
            assert shlex.split(stored_simple)[0] == simple_value

        # Also verify boolean handling
        local_env_bool = {}
        hooks._set_hook_env_var(
            local_env_bool, hooks.TFHookVarType.EXTRA, "flag", True, b64_encode
        )
        stored_flag = local_env_bool["TF_EXTRA_FLAG"]
        if b64_encode:
            # booleans base64-encode to their JSON literal for non-strings
            assert base64.b64decode(stored_flag).decode() == "true"
        else:
            # non-b64 keeps TRUE uppercasing
            assert shlex.split(stored_flag)[0] == "TRUE"

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
            "hook_script pre plan",
            cwd="working_dir/hooks",
            env={},
            stream_output=False,
            stdin=None,
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
    @mock.patch("tfworker.util.hooks._parse_tfvars_file")
    def test_populate_environment_with_terraform_variables(
        self, mock_parse_tfvars, mock_isfile
    ):
        """Test that variables from .tfvars are properly parsed and set"""
        mock_parse_tfvars.return_value = {
            "key": "value",
            "another_key": "another_value",
        }
        local_env = {}
        hooks._populate_environment_with_terraform_variables(
            local_env, "working_dir", "terraform_path", False
        )
        assert "TF_VAR_KEY" in local_env
        assert "TF_VAR_ANOTHER_KEY" in local_env
        mock_parse_tfvars.assert_called_once()

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
            local_env, "working_dir", "terraform_path", False, None
        )
        assert "TF_REMOTE_LOCAL_KEY" in local_env.keys()
        assert "TF_REMOTE_LOCAL_ANOTHER_KEY" in local_env.keys()
        assert local_env["TF_REMOTE_LOCAL_KEY"] == "value"
        assert local_env["TF_REMOTE_LOCAL_ANOTHER_KEY"] == "another_value"

    @mock.patch("builtins.open", new_callable=mock.mock_open)
    @mock.patch("tfworker.util.hooks.os.path.isfile", return_value=True)
    @mock.patch(
        "tfworker.util.hooks.get_state_item",
        side_effect=[
            '{"value":"staging","type":"string"}',
            '{"value":true,"type":"bool"}',
            '{"value":{"key":"val"},"type":"object"}',
            '{"value":["a","b","c"],"type":"list"}',
        ],
    )
    def test_populate_environment_with_terraform_remote_vars_realistic_json(
        self, mock_get_state_item, mock_isfile, mock_open
    ):
        """Test that remote vars correctly extract values from Terraform's JSON output format."""
        mock_terraform_locals = """
            local_key = data.terraform_remote_state.remote1.outputs.environment
            local_flag = data.terraform_remote_state.remote2.outputs.enabled
            local_config = data.terraform_remote_state.remote3.outputs.config
            local_items = data.terraform_remote_state.remote4.outputs.items
        """
        mock_open.return_value.read.return_value = mock_terraform_locals

        local_env = {}
        hooks._populate_environment_with_terraform_remote_vars(
            local_env, "working_dir", "terraform_path", False, None
        )

        # Simple string should be unquoted
        assert "TF_REMOTE_LOCAL_KEY" in local_env.keys()
        assert local_env["TF_REMOTE_LOCAL_KEY"] == "staging"

        # Boolean should be uppercase
        assert "TF_REMOTE_LOCAL_FLAG" in local_env.keys()
        assert local_env["TF_REMOTE_LOCAL_FLAG"] == "TRUE"

        # Complex object should be JSON-encoded and shlex-escaped
        assert "TF_REMOTE_LOCAL_CONFIG" in local_env.keys()
        # shlex.quote wraps JSON in single quotes
        assert local_env["TF_REMOTE_LOCAL_CONFIG"] == """'{"key":"val"}'"""

        # List should be JSON-encoded and shlex-escaped
        assert "TF_REMOTE_LOCAL_ITEMS" in local_env.keys()
        assert local_env["TF_REMOTE_LOCAL_ITEMS"] == """'["a","b","c"]'"""

    def test_populate_environment_with_extra_vars(self):
        local_env = {}
        extra_vars = {"extra_key": "extra_value"}
        hooks._populate_environment_with_extra_vars(local_env, extra_vars, False)
        assert "TF_EXTRA_EXTRA_KEY" in local_env
