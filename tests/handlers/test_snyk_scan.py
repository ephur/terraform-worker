import pytest
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from tfworker.definitions import Definition
from tfworker.exceptions import HandlerError
from tfworker.commands.terraform import TerraformResult
from tfworker.types import TerraformAction, TerraformStage
from tfworker.handlers import SnykConfig, SnykHandler

mock_path_exists = Mock()
mock_path_exists_fn = Mock(return_value=True)
mock_path_exists.exists = mock_path_exists_fn

mock_path_not_exists = Mock()
mock_path_not_exists_fn = Mock(return_value=False)
mock_path_not_exists.exists = mock_path_not_exists_fn

mock_access_allowed = Mock(return_value=True)    
mock_access_denied = Mock(return_value=False)

mock_environ = MagicMock()
mock_environ_dict = {'SNYK_TOKEN': "1234567890"}
mock_environ.__getitem__.side_effect = mock_environ_dict.__getitem__

mock_environ_no_token = MagicMock()
mock_environ_no_token.__getitem__.side_effect = {'SNYK_TOKEN': None}.__getitem__

tmp_dir = f"{Path.cwd()}/.pytest-tmp"
snyk_path = f"{tmp_dir}/snyk"
working_dir = f"{tmp_dir}/terraform-worker/tests/snyk"
plan_dir = f"{tmp_dir}/plans"
plan_path = f"{plan_dir}/my-test-def.tfplan"
definition_dir = f"{working_dir}/definitions"
scripts_dir = f"{Path.cwd()}/tests/helpers/scripts"
# Strange things happen when we mock the subprocess module
# Copy success/failure scripts in its place to simulate
mock_snyk_success_path = f"{scripts_dir}/mock_snyk_success.sh"
mock_snyk_failure_path = f"{scripts_dir}/mock_snyk_failure.sh"
@pytest.fixture(autouse=True)
def required_file_system_paths():
  Path.mkdir(Path(tmp_dir), 0o777, exist_ok=True)
  Path.mkdir(f"{tmp_dir}/plans", 0o777, parents=False, exist_ok=True)
  Path(plan_path).touch(0o777)
  Path.mkdir(Path(definition_dir), mode=0o777, parents=True, exist_ok=True)
  yield
  shutil.rmtree("./.pytest-tmp")

class TestSnykHandler:
    @patch('os.path', mock_path_exists)
    @patch('os.access', mock_access_allowed)
    @patch('os.environ', mock_environ)
    def test_init(self):
        config = Mock()
        config.model_fields = ["path", "my_custom_field", "required"]
        config.path = "/usr/bin/snyk"
        config.my_custom_field = "abc123"
        config.required = True

        handler = SnykHandler(config)
        assert handler._path == "/usr/bin/snyk"
        assert handler._my_custom_field == "abc123"
        assert handler._required == True
        mock_path_exists_fn.assert_called
        mock_access_allowed.assert_called

    @patch('os.path', mock_path_not_exists)
    @patch('os.access', mock_access_allowed)
    def test_init_path_not_exists(self):
        config = Mock()
        config.model_fields = ["path", "required"]
        config.path = "/usr/bin/snyk"
        config.required = True

        with pytest.raises(HandlerError):
          handler = SnykHandler(config)
          assert handler._path == "/usr/bin/snyk"
          assert handler._required == True
          mock_path_not_exists_fn.assert_called
          mock_access_allowed.assert_not_called

    @patch('os.path', mock_path_exists)
    @patch('os.access', mock_access_denied)
    def test_init_path_not_allowed(self):
        config = Mock()
        config.model_fields = ["path", "required"]
        config.path = "/usr/bin/snyk"
        config.required = True

        with pytest.raises(HandlerError):
          handler = SnykHandler(config)
          assert handler._path == "/usr/bin/snyk"
          assert handler._required == True
          mock_path_exists_fn.assert_called
          mock_access_denied.assert_called

    @patch('os.path', mock_path_exists)
    @patch('os.access', mock_access_allowed)
    @patch('os.environ', mock_environ_no_token)
    def test_init_missing_token(self):
        config = Mock()
        config.model_fields = ["path", "required"]
        config.path = "/usr/bin/snyk"
        config.required = True

        with pytest.raises(HandlerError):
          handler = SnykHandler(config)
          assert handler._path == "/usr/bin/snyk"
          assert handler._required == True
          mock_path_exists_fn.assert_called
          mock_access_allowed.assert_called

    @patch('os.environ', mock_environ)
    def test_execute_pre_plan(self):
        # Mock out the snyk call with a successful script
        shutil.copy(Path(mock_snyk_success_path), Path(snyk_path))
        
        config = SnykConfig(path=snyk_path, required=True)
        handler = SnykHandler(config)
        handler.execute(
            action=TerraformAction.PLAN,
            stage=TerraformStage.PRE,
            deployment="apps/dev",
            definition=Definition(name='test_def', path="apps-dev/test-def"),
            working_dir=working_dir,
        )

    @patch('os.environ', mock_environ)
    def test_execute_pre_plan_fail(self):
        # Mock out the snyk call with a failure script
        shutil.copy(Path(mock_snyk_failure_path), Path(snyk_path))
        
        config = SnykConfig(path=snyk_path, required=True)

        with pytest.raises(HandlerError):
            SnykHandler(config).execute(
                action=TerraformAction.PLAN,
                stage=TerraformStage.PRE,
                deployment="apps/dev",
                definition=Definition(name='test_def', path="apps-dev/test-def"),
                working_dir=working_dir,
            )

    @patch('os.environ', mock_environ)
    def test_execute_post_plan_no_changes(self):
        # Mock out the snyk call with a failre script
        # We shouldn't end up calling it, so it should fail if we do
        shutil.copy(Path(mock_snyk_failure_path), Path(snyk_path))
        
        result = TerraformResult(0, "Plan has no changes".encode("utf-8"), "".encode("utf-8"))
        config = SnykConfig(path=snyk_path, required=True)
        handler = SnykHandler(config)
        handler.execute(
            action=TerraformAction.PLAN,
            stage=TerraformStage.POST,
            deployment="apps/dev",
            definition=Definition(name='test_def', path="apps-dev/test-def"),
            working_dir=working_dir,
            result=result
        )

    @patch('os.environ', mock_environ)
    def test_execute_post_plan_changes_fail(self):
        # Mock out the snyk call with a faliure script
        # We want to ensure it got called, so we'll expect the failure as an exception
        shutil.copy(Path(mock_snyk_failure_path), Path(snyk_path))
        
        result = TerraformResult(2, "Plan has changes".encode("utf-8"), "".encode("utf-8"))
        config = SnykConfig(path=snyk_path, required=True)
        handler = SnykHandler(config)

        with pytest.raises(HandlerError):
            handler.execute(
                action=TerraformAction.PLAN,
                stage=TerraformStage.POST,
                deployment="apps/dev",
                definition=Definition(name='test_def', path="apps-dev/test-def", plan_file=plan_path),
                working_dir=working_dir,
                result=result
            )

    @patch('os.environ', mock_environ)
    @patch('os.access', mock_access_allowed)
    @patch('os.environ', mock_environ_no_token)
    def test_verify_snyk_args(self):
      # Mock out the snyk call with a successful script
      shutil.copy(Path(mock_snyk_success_path), Path(snyk_path))
      config = SnykConfig(path=snyk_path)
      handler = SnykHandler(config)
      test_def_dir = f"{definition_dir}/my-test-def"
      args = handler._build_snyk_args(test_def_dir)
      assert args == [
          snyk_path,
          "iac",
          "test",
          test_def_dir
      ]