import pytest

from tfworker.definitions import Definition, DefinitionsCollection
from tfworker.definitions.prepare import DefinitionPrepare
from tfworker.exceptions import ReservedFileError, TFWorkerException


@pytest.fixture
def mock_definition():
    return Definition(name="def1", path="./path")


@pytest.fixture
def def_prepare(mock_app_state):
    definitions = DefinitionsCollection({"def1": {"path": "./path"}})
    mock_app_state.definitions = definitions
    mock_app_state.root_options.repository_path = "."
    return DefinitionPrepare(mock_app_state)


class TestDefinitionPrepareCopyFiles:
    def test_copy_files(self, mocker, def_prepare, mock_definition):
        """make sure copy files makes the right calls"""
        mock_get_copier = mocker.patch("tfworker.definitions.prepare.get_coppier")
        mock_copy = mocker.patch("tfworker.definitions.prepare.copy")
        def_prepare.copy_files(mock_definition.name)
        mock_get_copier.assert_called_once_with(
            mock_definition.path, def_prepare._app_state.root_options.repository_path
        )
        mock_copy.assert_called_once_with(
            copier=mock_get_copier.return_value,
            destination=mock_definition.get_target_path(
                def_prepare._app_state.working_dir
            ),
            options=mock_definition.remote_path_options.model_dump(),
        )

    def test_copy_files_no_copier(self, mocker, def_prepare, mock_definition):
        """make sure copy files raises an exception if no copier is found"""
        mocker.patch(
            "tfworker.definitions.prepare.get_coppier",
            side_effect=NotImplementedError(),
        )
        with pytest.raises(TFWorkerException):
            def_prepare.copy_files(mock_definition.name)

    def test_copy_files_reserved_file_error(self, mocker, def_prepare, mock_definition):
        """make sure copy files raises an exception if a reserved file is found"""
        mocker.patch("tfworker.definitions.prepare.get_coppier")
        mocker.patch(
            "tfworker.definitions.prepare.copy", side_effect=ReservedFileError()
        )
        with pytest.raises(TFWorkerException):
            def_prepare.copy_files(mock_definition.name)


class TestDefinitionPrepareWriteTemplates:
    def test_render_templates(self, mocker, def_prepare, mock_definition):
        """make sure render_templates makes the right calls"""
        mock_get_jinja_env = mocker.patch("tfworker.definitions.prepare.get_jinja_env")
        mock_jinja_env = mocker.MagicMock()
        mock_jinja_env.list_templates.return_value = ["template1.tf"]
        mock_get_jinja_env.return_value = mock_jinja_env
        mock_write_template_file = mocker.patch(
            "tfworker.definitions.prepare.write_template_file"
        )
        mock_get_template_vars = mocker.patch.object(
            def_prepare, "_get_template_vars", return_value={}
        )
        target_path = mock_definition.get_target_path(
            def_prepare._app_state.working_dir
        )
        def_prepare.render_templates(mock_definition.name)
        mock_get_template_vars.assert_called_once_with(mock_definition.name)
        mock_get_jinja_env.assert_called_once_with(
            template_path=target_path, jinja_globals=mock_get_template_vars.return_value
        )
        mock_write_template_file.assert_called_once_with(
            jinja_env=mock_get_jinja_env(),
            template_path=mock_definition.get_target_path(
                def_prepare._app_state.working_dir
            ),
            template_file="template1.tf",
        )


class TestDefinitionPrepareCreateLocalVars:
    def test_create_local_vars(
        self,
        mocker,
    ):
        """make sure the local vars file is created with expected content"""
