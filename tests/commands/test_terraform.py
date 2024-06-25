# import pathlib
# from typing import Tuple
# from unittest import mock
# from unittest.mock import MagicMock, patch

# import pytest
# from google.cloud.exceptions import NotFound

# import tfworker
# from tfworker.commands.terraform import TerraformCommand, TerraformError
# from tfworker.definitions import Definition
# from tfworker.handlers import HandlerError


# def mock_pipe_exec(
#     args: str,
#     stdin: str = None,
#     cwd: str = None,
#     env: list = None,
#     stream_output: bool = False,
# ):
#     return (0, "".encode(), "".encode())


# def mock_tf_version(args: str) -> Tuple[int, str, str]:
#     return (0, args.encode(), "".encode())


# @pytest.fixture(scope="function")
# def definition():
#     mock_definition = MagicMock(spec=Definition)
#     mock_definition.tag = "test_tag"
#     mock_definition.path = "/path/to/definition"
#     mock_definition.fs_path = pathlib.Path("/path/to/definition")
#     # mock_definition._plan_file = None
#     # mock_definition._ready_to_apply = False
#     return mock_definition


# @pytest.fixture(scope="function")
# def terraform_command(rootc):
#     return TerraformCommand(
#         rootc,
#         plan_file_path="/path/to/plan",
#         tf_plan=True,
#         deployment="deployment",
#         show_output=True,
#     )


# @pytest.fixture(scope="function")
# def terraform_destroy_command(rootc):
#     return TerraformCommand(
#         rootc,
#         plan_file_path="/path/to/plan",
#         tf_plan=True,
#         deployment="deployment",
#         show_output=True,
#         destroy=True,
#     )


# class TestTerraformCommand:
#     """These are legacy tests, and will be refactored away as work on the TerraformCommand class progresses."""

#     @pytest.mark.parametrize(
#         "method, tf_cmd, args",
#         [
#             (
#                 "init",
#                 "tf_12cmd",
#                 ["-input=false", "-no-color", "-plugin-dir"],
#             ),
#             (
#                 "plan",
#                 "tf_12cmd",
#                 ["-input=false", "-detailed-exitcode", "-no-color"],
#             ),
#             (
#                 "apply",
#                 "tf_12cmd",
#                 ["-input=false", "-no-color", "-auto-approve"],
#             ),
#             (
#                 "destroy",
#                 "tf_12cmd",
#                 ["-input=false", "-no-color", "-auto-approve"],
#             ),
#             (
#                 "init",
#                 "tf_13cmd",
#                 ["-input=false", "-no-color", "-plugin-dir"],
#             ),
#             (
#                 "plan",
#                 "tf_13cmd",
#                 ["-input=false", "-detailed-exitcode", "-no-color"],
#             ),
#             (
#                 "apply",
#                 "tf_13cmd",
#                 ["-input=false", "-no-color", "-auto-approve"],
#             ),
#             (
#                 "destroy",
#                 "tf_13cmd",
#                 ["-input=false", "-no-color", "-auto-approve"],
#             ),
#             (
#                 "init",
#                 "tf_14cmd",
#                 ["-input=false", "-no-color", "-plugin-dir"],
#             ),
#             (
#                 "plan",
#                 "tf_14cmd",
#                 ["-input=false", "-detailed-exitcode", "-no-color"],
#             ),
#             (
#                 "apply",
#                 "tf_14cmd",
#                 ["-input=false", "-no-color", "-auto-approve"],
#             ),
#             (
#                 "destroy",
#                 "tf_14cmd",
#                 ["-input=false", "-no-color", "-auto-approve"],
#             ),
#             (
#                 "init",
#                 "tf_15cmd",
#                 ["-input=false", "-no-color", "-plugin-dir"],
#             ),
#             (
#                 "plan",
#                 "tf_15cmd",
#                 ["-input=false", "-detailed-exitcode", "-no-color"],
#             ),
#             (
#                 "apply",
#                 "tf_15cmd",
#                 ["-input=false", "-no-color", "-auto-approve"],
#             ),
#             (
#                 "destroy",
#                 "tf_15cmd",
#                 ["-input=false", "-no-color", "-auto-approve"],
#             ),
#         ],
#     )
#     def test_run(self, tf_cmd: str, method: callable, args: list, request):
#         tf_cmd = request.getfixturevalue(tf_cmd)
#         with mock.patch(
#             "tfworker.commands.terraform.pipe_exec",
#             side_effect=mock_pipe_exec,
#         ) as mocked:
#             tf_cmd._run(
#                 tf_cmd.definitions["test"],
#                 method,
#             )
#             mocked.assert_called_once()
#             call_as_string = str(mocked.mock_calls.pop())
#             assert method in call_as_string
#             for arg in args:
#                 assert arg in call_as_string

#     def test_worker_options(self, tf_13cmd_options):
#         # Verify that the options from the CLI override the options from the config
#         assert tf_13cmd_options._rootc.worker_options_odict.get("backend") == "s3"
#         assert tf_13cmd_options.backend.tag == "gcs"

#         # Verify that None options are overriden by the config
#         assert tf_13cmd_options._rootc.worker_options_odict.get("b64_encode") is True
#         assert tf_13cmd_options._args_dict.get("b64_encode") is False

#         # The fixture causes which to return /usr/local/bin/terraform.  However, since the
#         # path is specified in the worker_options, assert the value fromt he config.
#         assert tf_13cmd_options._terraform_bin == "/home/test/bin/terraform"

#     # def test_no_create_backend_bucket_fails_s3(self, rootc_no_create_backend_bucket):
#     #     with pytest.raises(BackendError):
#     #         with mock.patch(
#     #             "tfworker.commands.base.BaseCommand.get_terraform_version",
#     #             side_effect=lambda x: (13, 3),
#     #         ):
#     #             with mock.patch(
#     #                 "tfworker.commands.base.which",
#     #                 side_effect=lambda x: "/usr/local/bin/terraform",
#     #             ):
#     #                 return tfworker.commands.base.BaseCommand(
#     #                     rootc_no_create_backend_bucket, "test-0001", tf_version_major=13
#     #                 )

#     def test_no_create_backend_bucket_fails_gcs(self, grootc_no_create_backend_bucket):
#         with pytest.raises(SystemExit):
#             with mock.patch(
#                 "tfworker.commands.base.get_terraform_version",
#                 side_effect=lambda x: (13, 3),
#             ):
#                 with mock.patch(
#                     "tfworker.commands.base.which",
#                     side_effect=lambda x: "/usr/local/bin/terraform",
#                 ):
#                     with mock.patch(
#                         "tfworker.backends.gcs.storage.Client.from_service_account_json"
#                     ) as ClientMock:
#                         instance = ClientMock.return_value
#                         instance.get_bucket.side_effect = NotFound("bucket not found")
#                         return tfworker.commands.base.BaseCommand(
#                             grootc_no_create_backend_bucket,
#                             "test-0001",
#                             tf_version_major=13,
#                         )


# ####
# class TestTerraformCommandInit:
#     base_kwargs = {
#         "backend": "s3",
#         "backend_plans": False,
#         "b64_encode": False,
#         "color": True,
#         "deployment": "test_deployment",
#         "destroy": False,
#         "force": False,
#         "plan_file_path": None,
#         "provider_cache": "/path/to/cache",
#         "show_output": True,
#         "stream_output": False,
#         "terraform_bin": "/path/to/terraform",
#         "terraform_modules_dir": "/path/to/modules",
#         "tf_apply": True,
#         "tf_plan": True,
#         "tf_version": (1, 8),
#     }

#     @pytest.fixture
#     def terraform_command_class(self):
#         return TerraformCommand

#     def test_constructor_with_valid_arguments(self, rootc, terraform_command_class):
#         kwargs = self.base_kwargs.copy()

#         with patch.object(
#             terraform_command_class, "_resolve_arg", side_effect=lambda arg: kwargs[arg]
#         ):
#             command = terraform_command_class(rootc, **kwargs)
#             assert command._destroy == kwargs["destroy"]
#             assert command._tf_apply == kwargs["tf_apply"]
#             assert command._tf_plan == kwargs["tf_plan"]
#             assert command._plan_file_path == kwargs["plan_file_path"]
#             assert command._b64_encode == kwargs["b64_encode"]
#             assert command._deployment == kwargs["deployment"]
#             assert command._force == kwargs["force"]
#             assert command._show_output == kwargs["show_output"]
#             assert command._stream_output == kwargs["stream_output"]
#             assert command._use_colors is True
#             assert command._terraform_modules_dir == kwargs["terraform_modules_dir"]
#             assert command._terraform_output == {}

#     def test_constructor_with_apply_and_destroy(self, rootc, terraform_command_class):
#         kwargs = self.base_kwargs.copy()
#         kwargs["tf_apply"] = True
#         kwargs["destroy"] = True

#         with patch.object(
#             terraform_command_class, "_resolve_arg", side_effect=lambda arg: kwargs[arg]
#         ):
#             with patch("click.secho") as mock_secho, pytest.raises(SystemExit):
#                 terraform_command_class(rootc, **kwargs)
#                 mock_secho.assert_called_with(
#                     "Cannot apply and destroy in the same run", fg="red"
#                 )

#     def test_constructor_with_backend_plans(self, rootc, terraform_command_class):
#         kwargs = self.base_kwargs.copy()
#         kwargs["backend_plans"] = True

#         with patch.object(
#             terraform_command_class, "_resolve_arg", side_effect=lambda arg: kwargs[arg]
#         ):
#             with patch("pathlib.Path.mkdir") as mock_mkdir:
#                 command = terraform_command_class(rootc, **kwargs)
#                 assert command._plan_file_path == f"{command._temp_dir}/plans"
#                 mock_mkdir.assert_called_once_with(parents=True, exist_ok=True)


# class TestTerraformCommandProperties:
#     def test_plan_for_apply(self, terraform_command):
#         assert terraform_command.plan_for == "apply"

#     def test_plan_for_destroy(self, terraform_destroy_command):
#         assert terraform_destroy_command.plan_for == "destroy"

#     def test_tf_version_major(self, terraform_command):
#         assert terraform_command.tf_version_major == 1


# class TestTerraformCommandExec:

#     def test_exec_valid_flow(self, terraform_command, definition):
#         def_iter = [definition]
#         terraform_command._provider_cache = "/path/to/cache"

#         with patch.object(
#             terraform_command.definitions, "limited", return_value=def_iter
#         ), patch(
#             "tfworker.commands.terraform.tf_util.mirror_providers"
#         ) as mock_mirror_providers, patch(
#             "tfworker.commands.terraform.tf_util.prep_modules"
#         ) as mock_prep_modules, patch.object(
#             terraform_command, "_prep_and_init"
#         ) as mock_prep_and_init, patch.object(
#             terraform_command, "_check_plan", return_value=True
#         ) as mock_check_plan, patch.object(
#             terraform_command, "_exec_plan", return_value="changes"
#         ) as mock_exec_plan, patch.object(
#             terraform_command, "_check_apply_or_destroy", return_value=True
#         ) as mock_check_apply_or_destroy, patch.object(
#             terraform_command, "_exec_apply_or_destroy"
#         ) as mock_exec_apply_or_destroy:

#             terraform_command.exec()

#             mock_mirror_providers.assert_called_once()
#             mock_prep_modules.assert_called_once_with(
#                 terraform_command._terraform_modules_dir,
#                 terraform_command._temp_dir,
#                 required=True,
#             )
#             mock_prep_and_init.assert_called_once_with(def_iter)
#             mock_check_plan.assert_called_once_with(definition)
#             mock_exec_plan.assert_called_once_with(definition)
#             mock_check_apply_or_destroy.assert_called_once_with("changes", definition)
#             mock_exec_apply_or_destroy.assert_called_once_with(definition)

#     def test_exec_with_invalid_limit(self, terraform_command):
#         with patch.object(
#             terraform_command.definitions,
#             "limited",
#             side_effect=ValueError("Invalid limit"),
#         ), patch("click.secho") as mock_secho:
#             with pytest.raises(SystemExit):
#                 terraform_command.exec()
#                 mock_secho.assert_called_once_with(
#                     "Error with supplied limit: Invalid limit", fg="red"
#                 )

#     def test_exec_without_plan(self, terraform_command, definition):
#         def_iter = [definition]

#         with patch.object(
#             terraform_command.definitions, "limited", return_value=def_iter
#         ), patch(
#             "tfworker.commands.terraform.tf_util.prep_modules"
#         ) as mock_prep_modules, patch.object(
#             terraform_command, "_prep_and_init"
#         ) as mock_prep_and_init, patch.object(
#             terraform_command, "_check_plan", return_value=False
#         ) as mock_check_plan, patch.object(
#             terraform_command, "_exec_plan"
#         ) as mock_exec_plan, patch.object(
#             terraform_command, "_check_apply_or_destroy", return_value=True
#         ) as mock_check_apply_or_destroy, patch.object(
#             terraform_command, "_exec_apply_or_destroy"
#         ) as mock_exec_apply_or_destroy:

#             terraform_command.exec()

#             mock_prep_modules.assert_called_once_with(
#                 terraform_command._terraform_modules_dir,
#                 terraform_command._temp_dir,
#                 required=True,
#             )
#             mock_prep_and_init.assert_called_once_with(def_iter)
#             mock_check_plan.assert_called_once_with(definition)
#             mock_exec_plan.assert_not_called()
#             mock_check_apply_or_destroy.assert_called_once_with(None, definition)
#             mock_exec_apply_or_destroy.assert_called_once_with(definition)

#     def test_exec_with_no_apply_or_destroy(self, terraform_command, definition):
#         def_iter = [definition]

#         with patch.object(
#             terraform_command.definitions, "limited", return_value=def_iter
#         ), patch(
#             "tfworker.commands.terraform.tf_util.prep_modules"
#         ) as mock_prep_modules, patch.object(
#             terraform_command, "_prep_and_init"
#         ) as mock_prep_and_init, patch.object(
#             terraform_command, "_check_plan", return_value=True
#         ) as mock_check_plan, patch.object(
#             terraform_command, "_exec_plan", return_value="changes"
#         ) as mock_exec_plan, patch.object(
#             terraform_command, "_check_apply_or_destroy", return_value=False
#         ) as mock_check_apply_or_destroy, patch.object(
#             terraform_command, "_exec_apply_or_destroy"
#         ) as mock_exec_apply_or_destroy:

#             terraform_command.exec()

#             mock_prep_modules.assert_called_once_with(
#                 terraform_command._terraform_modules_dir,
#                 terraform_command._temp_dir,
#                 required=True,
#             )
#             mock_prep_and_init.assert_called_once_with(def_iter)
#             mock_check_plan.assert_called_once_with(definition)
#             mock_exec_plan.assert_called_once_with(definition)
#             mock_check_apply_or_destroy.assert_called_once_with("changes", definition)
#             mock_exec_apply_or_destroy.assert_not_called()

#     def test_exec_with_required_prep_modules(self, terraform_command, definition):
#         terraform_command._terraform_modules_dir = "/temp/path"
#         def_iter = [definition]

#         with patch.object(
#             terraform_command.definitions, "limited", return_value=def_iter
#         ), patch(
#             "tfworker.commands.terraform.tf_util.prep_modules"
#         ) as mock_prep_modules, patch.object(
#             terraform_command, "_prep_and_init"
#         ) as mock_prep_and_init, patch.object(
#             terraform_command, "_check_plan", return_value=True
#         ) as mock_check_plan, patch.object(
#             terraform_command, "_exec_plan", return_value="changes"
#         ) as mock_exec_plan, patch.object(
#             terraform_command, "_check_apply_or_destroy", return_value=True
#         ) as mock_check_apply_or_destroy, patch.object(
#             terraform_command, "_exec_apply_or_destroy"
#         ) as mock_exec_apply_or_destroy:

#             terraform_command.exec()

#             mock_prep_modules.assert_called_once_with(
#                 terraform_command._terraform_modules_dir,
#                 terraform_command._temp_dir,
#                 required=True,
#             )
#             mock_prep_and_init.assert_called_once_with(def_iter)
#             mock_check_plan.assert_called_once_with(definition)
#             mock_exec_plan.assert_called_once_with(definition)
#             mock_check_apply_or_destroy.assert_called_once_with("changes", definition)
#             mock_exec_apply_or_destroy.assert_called_once_with(definition)


# class TestTerraformCommandPrepAndInit:

#     def test_prep_and_init_valid_flow(self, terraform_command, definition):
#         def_iter = [definition]

#         with patch("click.secho") as mock_secho, patch.object(
#             definition, "prep"
#         ) as mock_prep, patch.object(terraform_command, "_run") as mock_run:

#             terraform_command._prep_and_init(def_iter)

#             mock_secho.assert_any_call(
#                 f"preparing definition: {definition.tag}", fg="green"
#             )
#             mock_prep.assert_called_once_with(terraform_command._backend)
#             mock_run.assert_called_once_with(
#                 definition, "init", debug=terraform_command._show_output
#             )

#     def test_prep_and_init_with_terraform_error(self, terraform_command, definition):
#         def_iter = [definition]

#         with patch("click.secho") as mock_secho, patch.object(
#             definition, "prep"
#         ) as mock_prep, patch.object(
#             terraform_command, "_run", side_effect=TerraformError
#         ) as mock_run:

#             with pytest.raises(SystemExit):
#                 terraform_command._prep_and_init(def_iter)

#             mock_secho.assert_any_call(
#                 f"preparing definition: {definition.tag}", fg="green"
#             )
#             mock_prep.assert_called_once_with(terraform_command._backend)
#             mock_run.assert_called_once_with(
#                 definition, "init", debug=terraform_command._show_output
#             )
#             mock_secho.assert_any_call("error running terraform init", fg="red")


# class TestTerraformCommandPlanFunctions:
#     def test_handle_no_plan_path_true(self, terraform_command, definition):
#         terraform_command._tf_plan = False
#         assert terraform_command._handle_no_plan_path(definition) is False
#         assert definition._ready_to_apply is True

#     def test_handle_no_plan_path_false(self, terraform_command, definition):
#         terraform_command._tf_plan = True
#         assert terraform_command._handle_no_plan_path(definition) is True
#         assert definition._ready_to_apply is False

#     def test_prepare_plan_file(self, terraform_command, definition):
#         plan_file = terraform_command._prepare_plan_file(definition)
#         assert definition.plan_file == plan_file
#         assert plan_file == pathlib.Path("/path/to/plan/deployment_test_tag.tfplan")

#     def test_validate_plan_path_valid(self, terraform_command):
#         with patch("pathlib.Path.exists", return_value=True), patch(
#             "pathlib.Path.is_dir", return_value=True
#         ):
#             terraform_command._validate_plan_path(pathlib.Path("/valid/path"))

#     def test_validate_plan_path_invalid(self, terraform_command):
#         with patch("pathlib.Path.exists", return_value=False), patch(
#             "pathlib.Path.is_dir", return_value=False
#         ), pytest.raises(SystemExit):
#             terraform_command._validate_plan_path(pathlib.Path("/invalid/path"))

#     def test_run_handlers(self, terraform_command, definition):
#         with patch.object(
#             terraform_command, "_execute_handlers", return_value=None
#         ) as mock_execute_handlers:
#             terraform_command._run_handlers(
#                 definition, "plan", "check", pathlib.Path("/path/to/planfile")
#             )
#             mock_execute_handlers.assert_called_once_with(
#                 action="plan",
#                 stage="check",
#                 deployment="deployment",
#                 definition=definition.tag,
#                 definition_path=definition.fs_path,
#                 planfile=pathlib.Path("/path/to/planfile"),
#             )

#     def test_run_handlers_with_error(self, terraform_command, definition):
#         error = HandlerError("Handler failed")
#         error.terminate = False
#         with patch.object(
#             terraform_command, "_execute_handlers", side_effect=error
#         ), patch("click.secho"):
#             terraform_command._run_handlers(
#                 definition, "plan", "check", pathlib.Path("/path/to/planfile")
#             )

#     def test_run_handlers_with_fatal_error(self, terraform_command, definition):
#         error = HandlerError("Fatal handler error")
#         error.terminate = True
#         with patch.object(
#             terraform_command, "_execute_handlers", side_effect=error
#         ), patch("click.secho"), pytest.raises(SystemExit):
#             terraform_command._run_handlers(
#                 definition, "plan", "check", pathlib.Path("/path/to/planfile")
#             )

#     def test_should_plan_no_tf_plan(self, terraform_command, definition):
#         terraform_command._tf_plan = False
#         plan_file = pathlib.Path("/path/to/empty.tfplan")
#         assert terraform_command._should_plan(definition, plan_file) is False
#         assert definition._ready_to_apply is True

#     def test_should_plan_empty_plan_file(self, terraform_command, definition):
#         plan_file = pathlib.Path("/path/to/empty.tfplan")
#         with patch("pathlib.Path.exists", return_value=True), patch(
#             "pathlib.Path.stat", return_value=MagicMock(st_size=0)
#         ):
#             assert terraform_command._should_plan(definition, plan_file) is True
#             assert definition._ready_to_apply is False

#     def test_should_plan_existing_valid_plan_file(self, terraform_command, definition):
#         plan_file = pathlib.Path("/path/to/valid.tfplan")
#         with patch("pathlib.Path.exists", return_value=True), patch(
#             "pathlib.Path.stat", return_value=MagicMock(st_size=100)
#         ):
#             assert terraform_command._should_plan(definition, plan_file) is False
#             assert definition._ready_to_apply is True

#     def test_should_plan_no_existing_plan_file(self, terraform_command, definition):
#         plan_file = pathlib.Path("/path/to/nonexistent.tfplan")
#         with patch("pathlib.Path.exists", return_value=False):
#             assert terraform_command._should_plan(definition, plan_file) is True
#             assert definition._ready_to_apply is False

#     def test_check_plan_no_plan_path(self, terraform_command, definition):
#         terraform_command._plan_file_path = None
#         with patch.object(
#             terraform_command, "_handle_no_plan_path", return_value=False
#         ) as mock_handle_no_plan_path:
#             assert terraform_command._check_plan(definition) is False
#             mock_handle_no_plan_path.assert_called_once_with(definition)

#     def test_check_plan_with_plan_path(self, terraform_command, definition):
#         plan_file = pathlib.Path("/path/to/plan/deployment_test_tag.tfplan")
#         with patch.object(
#             terraform_command, "_prepare_plan_file", return_value=plan_file
#         ) as mock_prepare_plan_file, patch.object(
#             terraform_command, "_validate_plan_path"
#         ) as mock_validate_plan_path, patch.object(
#             terraform_command, "_run_handlers"
#         ) as mock_run_handlers, patch.object(
#             terraform_command, "_should_plan", return_value=True
#         ) as mock_should_plan:
#             assert terraform_command._check_plan(definition) is True
#             mock_prepare_plan_file.assert_called_once_with(definition)
#             mock_validate_plan_path.assert_called_once_with(plan_file.parent)
#             mock_run_handlers.assert_called_once()
#             mock_should_plan.assert_called_once_with(definition, plan_file)


# if __name__ == "__main__":
#     pytest.main()
