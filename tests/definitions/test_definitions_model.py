import pytest

from tfworker.definitions.model import Definition, DefinitionRemoteOptions


def mock_definition():
    return {
        "name": "test",
        "path": "test",
    }


@pytest.fixture
def mock_global_vars():
    return {
        "terraform_vars": {
            "global_tf_1": "global1",
            "global_tf_2": "global2",
        },
        "remote_vars": {
            "global_remote_1": "global1",
            "global_remote_2": "global2",
        },
        "template_vars": {
            "global_template_1": "global1",
            "global_template_2": "global2",
        },
    }


class TestDefinitionModel:
    def test_definition_model(self):
        testdef = Definition(**mock_definition())
        assert testdef.name == "test"
        assert testdef.path == "test"
        assert testdef.ready is False
        assert testdef.needs_apply is False
        assert testdef.plan_file is None

    def test_definition_path(self, tmp_path):
        testdef = Definition(**mock_definition())
        assert testdef.get_target_path(tmp_path) == tmp_path / "definitions" / "test"

    def test_definition_template_vars(self):
        testdef = Definition(**mock_definition())
        testdef.template_vars = {"test": "test"}
        assert testdef.get_template_vars({}) == {"test": "test"}

    def test_definition_template_vars_with_globals(self, mock_global_vars):
        testdef = Definition(**mock_definition())
        testdef.template_vars = {"test": "test"}
        expected_result = {**mock_global_vars["template_vars"], "test": "test"}
        assert (
            testdef.get_template_vars(mock_global_vars["template_vars"])
            == expected_result
        )

    def test_definition_template_vars_ignore_global_vars(self, mock_global_vars):
        testdef = Definition(**mock_definition())
        testdef.ignore_global_vars = True
        testdef.template_vars = {"test": "test"}
        assert testdef.get_template_vars(mock_global_vars["template_vars"]) == {
            "test": "test"
        }

    def test_definition_template_vars_ignore_template_vars(self, mock_global_vars):
        testdef = Definition(**mock_definition())
        testdef.ignored_global_template_vars = ["global_template_1"]
        testdef.template_vars = {"test": "test"}
        assert testdef.get_template_vars(mock_global_vars["template_vars"]) == {
            "test": "test",
            "global_template_2": "global2",
        }

    def test_definition_template_vars_use_global_template_vars(self, mock_global_vars):
        testdef = Definition(**mock_definition())
        testdef.use_global_template_vars = ["global_template_1"]
        testdef.template_vars = {"test": "test"}
        assert testdef.get_template_vars(mock_global_vars["template_vars"]) == {
            "test": "test",
            "global_template_1": "global1",
        }

    def test_definition_template_vars_precedence(self, mock_global_vars):
        testdef = Definition(**mock_definition())
        testdef.template_vars = {"global_template_1": "test"}
        assert testdef.get_template_vars(mock_global_vars["template_vars"]) == {
            "global_template_2": "global2",
            "global_template_1": "test",
        }

    def test_definition_remote_vars(self):
        testdef = Definition(**mock_definition())
        testdef.remote_vars = {"test": "test"}
        assert testdef.get_remote_vars({"test": "test"}) == {"test": "test"}

    def test_definition_remote_vars_with_globals(self, mock_global_vars):
        testdef = Definition(**mock_definition())
        testdef.remote_vars = {"test": "test"}
        expected_result = {**mock_global_vars["remote_vars"], "test": "test"}
        assert (
            testdef.get_remote_vars(mock_global_vars["remote_vars"]) == expected_result
        )

    def test_definition_remote_vars_ignore_global_vars(self, mock_global_vars):
        testdef = Definition(**mock_definition())
        testdef.ignore_global_vars = True
        testdef.remote_vars = {"test": "test"}
        assert testdef.get_remote_vars(mock_global_vars["remote_vars"]) == {
            "test": "test"
        }

    def test_definition_remote_vars_ignore_remote_vars(self, mock_global_vars):
        testdef = Definition(**mock_definition())
        testdef.ignored_global_remote_vars = ["global_remote_1"]
        testdef.remote_vars = {"test": "test"}
        assert testdef.get_remote_vars(mock_global_vars["remote_vars"]) == {
            "test": "test",
            "global_remote_2": "global2",
        }

    def test_definition_remote_vars_use_global_remote_vars(self, mock_global_vars):
        testdef = Definition(**mock_definition())
        testdef.use_global_remote_vars = ["global_remote_1"]
        testdef.remote_vars = {"test": "test"}
        assert testdef.get_remote_vars(mock_global_vars["remote_vars"]) == {
            "test": "test",
            "global_remote_1": "global1",
        }

    def test_definition_remote_vars_precedence(self, mock_global_vars):
        testdef = Definition(**mock_definition())
        testdef.remote_vars = {"global_remote_1": "test"}
        assert testdef.get_remote_vars(mock_global_vars["remote_vars"]) == {
            "global_remote_2": "global2",
            "global_remote_1": "test",
        }

    def test_definition_terraform_vars(self):
        testdef = Definition(**mock_definition())
        testdef.terraform_vars = {"test": "test"}
        assert testdef.get_terraform_vars({}) == {"test": "test"}

    def test_definition_terraform_vars_with_globals(self, mock_global_vars):
        testdef = Definition(**mock_definition())
        testdef.terraform_vars = {"test": "test"}
        expected_result = {**mock_global_vars["terraform_vars"], "test": "test"}
        assert (
            testdef.get_terraform_vars(mock_global_vars["terraform_vars"])
            == expected_result
        )

    def test_definition_terraform_vars_ignore_global_vars(self, mock_global_vars):
        testdef = Definition(**mock_definition())
        testdef.ignore_global_vars = True
        testdef.terraform_vars = {"test": "test"}
        assert testdef.get_terraform_vars(mock_global_vars["terraform_vars"]) == {
            "test": "test"
        }

    def test_definition_terraform_vars_ignore_terraform_vars(self, mock_global_vars):
        testdef = Definition(**mock_definition())
        testdef.ignored_global_terraform_vars = ["global_tf_1"]
        testdef.terraform_vars = {"test": "test"}
        assert testdef.get_terraform_vars(mock_global_vars["terraform_vars"]) == {
            "test": "test",
            "global_tf_2": "global2",
        }

    def test_definition_terraform_vars_use_global_terraform_vars(
        self, mock_global_vars
    ):
        testdef = Definition(**mock_definition())
        testdef.use_global_terraform_vars = ["global_tf_1"]
        testdef.terraform_vars = {"test": "test"}
        assert testdef.get_terraform_vars(mock_global_vars["terraform_vars"]) == {
            "test": "test",
            "global_tf_1": "global1",
        }

    def test_definition_terraform_vars_precedence(self, mock_global_vars):
        testdef = Definition(**mock_definition())
        testdef.terraform_vars = {"global_tf_1": "test"}
        assert testdef.get_terraform_vars(mock_global_vars["terraform_vars"]) == {
            "global_tf_2": "global2",
            "global_tf_1": "test",
        }

    def test_definition_remote_options(self):
        testdef = DefinitionRemoteOptions(branch="test")
        assert testdef.branch == "test"
        assert testdef.sub_path is None

    def test_get_used_providers(self, mocker):
        mocker.patch(
            "tfworker.util.terraform.find_required_providers", return_value={"aws": ""}
        )
        testdef = Definition(**mock_definition())
        assert testdef.get_used_providers("working_dir") == ["aws"]

    def test_get_used_providers_no_providers(self, mocker):
        mocker.patch(
            "tfworker.util.terraform.find_required_providers",
            side_effect=AttributeError,
        )
        testdef = Definition(**mock_definition())
        assert testdef.get_used_providers("working_dir_two") is None
