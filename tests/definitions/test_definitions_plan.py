from pathlib import Path

from tfworker.definitions.model import Definition
from tfworker.definitions.plan import DefinitionPlan
from tfworker.types import TerraformAction

mock_definition = Definition(name="def1", path="./path")


class TestDefinitionsPlan:
    def test_plan_init(self, mock_click_context, mock_app_state):
        dp = DefinitionPlan(mock_click_context, mock_app_state)
        assert dp._ctx == mock_click_context
        assert dp._app_state == mock_app_state

    def test_plan_for_apply(self, mock_app_state):
        dp = DefinitionPlan(None, mock_app_state)
        assert dp.plan_for == TerraformAction.APPLY

    def test_plan_for_destroy(self, mock_app_state):
        mock_app_state.terraform_options.apply = False
        mock_app_state.terraform_options.destroy = True

        dp = DefinitionPlan(None, mock_app_state)
        assert dp.plan_for == TerraformAction.DESTROY

    def test_set_plan_file(self, mock_click_context, mock_app_state):
        dp = DefinitionPlan(mock_click_context, mock_app_state)
        dp.set_plan_file(mock_definition)
        assert mock_definition.plan_file == Path(
            f"{mock_app_state.working_dir}/plans/{mock_definition.name}.tfplan"
        )

    def test_set_plan_file_custom_path(
        self, mock_click_context, mock_app_state, tmpdir
    ):
        mock_app_state.terraform_options.plan_file_path = str(tmpdir)
        dp = DefinitionPlan(mock_click_context, mock_app_state)
        dp.set_plan_file(mock_definition)
        assert mock_definition.plan_file == Path(
            f"{str(tmpdir)}/{mock_app_state.deployment}/{mock_definition.name}.tfplan"
        )
        assert mock_definition.plan_file.parent.exists()

    def test_needs_plan(self, mock_click_context, mock_app_state):
        dp = DefinitionPlan(mock_click_context, mock_app_state)
        assert dp.needs_plan(mock_definition)[0] is True
        assert dp.needs_plan(mock_definition)[1].startswith("no saved")

    def test_needs_plan_empty_file(self, mock_click_context, mock_app_state):
        mock_app_state.root_options.backend_plans = True
        dp = DefinitionPlan(mock_click_context, mock_app_state)
        dp.set_plan_file(mock_definition)
        mock_definition.plan_file.touch()
        result = dp.needs_plan(mock_definition)
        assert result[0] is True
        assert result[1].startswith("empty")
        assert not mock_definition.plan_file.exists()

    def test_needs_plan_existing_file(self, mock_click_context, mock_app_state):
        mock_app_state.root_options.backend_plans = True
        dp = DefinitionPlan(mock_click_context, mock_app_state)
        dp.set_plan_file(mock_definition)
        mock_definition.plan_file.write_text("test")
        result = dp.needs_plan(mock_definition)
        assert result[0] is False
        assert result[1].startswith("plan file exists")
        assert mock_definition.plan_file.exists()

    def test_needs_plan_existing_file_no_file(self, mock_click_context, mock_app_state):
        mock_app_state.root_options.backend_plans = True
        dp = DefinitionPlan(mock_click_context, mock_app_state)
        dp.set_plan_file(mock_definition)
        result = dp.needs_plan(mock_definition)
        assert result[0] is True
        assert result[1].startswith("no plan file")
        assert not mock_definition.plan_file.exists()
