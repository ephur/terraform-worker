from pathlib import Path
from typing import TYPE_CHECKING, Tuple

from tfworker.types.terraform import TerraformAction

if TYPE_CHECKING:
    from click import Context

    from tfworker.app_state import AppState
    from tfworker.definitions.model import Definition


class DefinitionPlan:
    """
    DefinitionPlan is a class to help working with definitions to get everything
    ready to execute terraform plan, after terraform init has been run
    """

    def __init__(self, ctx: "Context", app_state: "AppState"):
        self._ctx: "Context" = ctx
        self._app_state: "AppState" = app_state

    @property
    def plan_for(self) -> TerraformAction:
        if self._app_state.terraform_options.destroy:
            return TerraformAction.DESTROY
        return TerraformAction.APPLY

    def set_plan_file(self, definition: "Definition") -> str:
        """
        Get the plan file for a definition

        Args:
            name (str): The name of the definition

        Returns
            str: The absolute path to the plan file
        """
        if self._app_state.terraform_options.plan_file_path:
            plan_base: str = Path(
                f"{self._app_state.terraform_options.plan_file_path}/{self._app_state.deployment}"
            ).resolve()
        else:
            plan_base: str = Path(f"{self._app_state.working_dir}/plans").resolve()

        plan_base.mkdir(parents=True, exist_ok=True)
        plan_file: Path = plan_base / f"{definition.name}.tfplan"
        definition.plan_file = plan_file.resolve()

    def needs_plan(self, definition: "Definition") -> Tuple[bool, str]:
        """
        Check if a definition needs a plan

        Args:
            name (str): The name of the definition

        Returns:
            Tuple[bool, str]: A tuple with a boolean indicating if a plan is needed
            and a string with the reason why a plan is or is not needed
        """
        # no saved plans possible
        if not (
            self._app_state.terraform_options.plan_file_path
            or self._app_state.root_options.backend_plans
        ):
            return True, "no saved plans possible"

        plan_file: Path = Path(definition.plan_file)

        if plan_file.exists() and plan_file.stat().st_size > 0:
            return False, "plan file exists"

        if plan_file.exists() and plan_file.stat().st_size == 0:
            plan_file.unlink()
            return True, "empty plan file"

        return True, "no plan file"
