import os
from pathlib import Path
from typing import TYPE_CHECKING, Union

from pydantic import BaseModel

import tfworker.util.log as log
from tfworker.exceptions import HandlerError
from tfworker.types.terraform import TerraformAction, TerraformStage
from tfworker.util.system import pipe_exec

from .base import BaseHandler
from .registry import HandlerRegistry

if TYPE_CHECKING:
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition


class SnykConfig(BaseModel):
    args: dict = {}
    cache_dir: str = "/tmp/snyk_cache"
    debug: bool = False
    exit_code: str = "1"
    handler_debug: bool = False
    path: str = "/usr/bin/snyk"
    quiet: bool = True
    required: bool = False
    severity: str = "HIGH,CRITICAL"
    skip_dirs: list = ["**/examples"]
    skip_planfile: bool = False
    skip_definition: bool = True
    stream_output: bool = True
    exempt_definitions: list = []


@HandlerRegistry.register("snyk")
class SnykHandler(BaseHandler):
    """
    The SnykHandler will execute a snyk scan on a specified terraform plan file
    """

    actions = [TerraformAction.PLAN]
    config_model = SnykConfig
    _ready = False
    default_priority = {
        TerraformAction.PLAN: 50,
    }

    def __init__(self, config: BaseModel) -> None:
        # configure the handler
        for k in config.model_fields:
            setattr(self, f"_{k}", getattr(config, k))

        # ensure snyk is runnable
        if not self._snyk_runable(self._path):
            if self._required:
                raise HandlerError(
                    f"Snyk is not runnable at {self._path}", terminate=True
                )
            raise HandlerError(f"Snyk is not runnable at {self._path}")

        self._ready = True

    def execute(
        self,
        action: "TerraformAction",
        stage: "TerraformStage",
        deployment: str,
        definition: "Definition",
        working_dir: str,
        result: Union["TerraformResult", None] = None,
    ) -> None:  # pragma: no cover
        """Execute the handler, running snyk against definition source to be planned (PRE), or the planfile (POST)

        Parameters:
            action (str): the action that triggered the handler (one of plan, clean, apply, destroy)
            stage (str): the stage of the action (one of pre, post)
            planfile (str): the path to the terraform plan file
            deployment (str): the name of the deployment
            definition (str): the name of the terraform definition
            working_dir (str): The cwd of the worker running the handler
            result (TerraformResult): The result of the action

        Returns:
            None
        """
        # pre plan; snyk scan the generated definition source
        if action == TerraformAction.PLAN and stage == TerraformStage.PRE:
            definition_path = definition.get_target_path(working_dir=working_dir)
            if definition_path is None:
                raise HandlerError(
                    "definition_path is not provided, can't scan",
                    terminate=self._required,
                )

            definition_name = definition.name
            if self._skip_definition or definition_name in self._exempt_definitions:
                log.info(f"Skipping snyk scan of definition: {definition_name}")
                return None

            log.info(f"scanning definition with snyk: {definition_path}")
            self._scan(definition, Path(definition_path))

        # post plan; snyk scan the planfile if its applicable
        if (
            action == TerraformAction.PLAN
            and stage == TerraformStage.POST
            and result.has_changes()
        ):
            planfile = definition.plan_file
            if planfile is None:
                raise HandlerError(
                    "planfile is not provided, can't scan", terminate=self._required
                )

            if self._skip_planfile:
                log.info(f"Skipping snyk scan of planfile: {planfile}")
                return None

            definition_name = definition.name
            if self._skip_definition or definition_name in self._exempt_definitions:
                log.info(f"Skipping snyk scan of definition: {definition_name}")
                return None

            jsonfile = Path(planfile).with_suffix(".tfplan.json")
            log.info(f"scanning planfile with snyk: {jsonfile}")
            self._scan(definition, Path(jsonfile))

    def _scan(self, definition: "Definition", target_path: Path = None):
        """Execute a snyk scan on the provided source or planfile

        Parameters:
            definition (Definition): the Definition model
            target_path (pathlib.Path): the path to the definition source (PRE) or planfile (POST)

        Returns:
            None
        """
        self._raise_if_not_ready()
        snyk_args = self._build_snyk_args(target_path)

        try:
            if self._debug:
                log.debug(f"cmd: {' '.join(snyk_args)}")
            (exit_code, stdout, stderr) = pipe_exec(
                f"{' '.join(snyk_args)}",
                cwd=target_path.parent,
                stream_output=self._stream_output,
            )
        except Exception as e:
            raise HandlerError(f"Error executing snyk scan: {e}")

        self._handle_results(exit_code, stdout, stderr, definition)

    def _build_snyk_args(self, target_path):
        snyk_args = []
        snyk_args.append(self._path)
        snyk_args.append("iac")
        snyk_args.append("test")
        snyk_args.append(str(Path(target_path).resolve()))
        return snyk_args

    def _handle_results(self, exit_code, stdout, stderr, definition):
        """Handle the results of the snyk scan

        Parameters:
            exit_code (int): the exit code of the snyk scan
            stdout (str): the stdout of the snyk scan
            stderr (str): the stderr of the snyk scan

        Returns:
            None
        """
        if exit_code == 0:
            log.debug(f"snyk scan exit code: {exit_code}")
            log.debug(f"stdout: {stdout.decode('UTF-8')}")
            log.debug(f"stderr: {stderr.decode('UTF-8')}")

        if exit_code != 0:
            log.error(f"snyk scan failed with exit code {exit_code}")
            log.error(f"stdout: {stdout.decode('UTF-8')}")
            log.error(f"stderr: {stderr.decode('UTF-8')}")

            if self._required:
                planfile = definition.plan_file
                if planfile is not None:
                    if Path(planfile).exists():
                        log.warn(f"Removing planfile: {planfile}")
                        os.remove(planfile)
                    jsonfile = Path(planfile).with_suffix(".tfplan.json")
                    if Path(jsonfile).exists():
                        log.warn(f"Removing JSON planfile: {jsonfile}")
                        os.remove(jsonfile)
                raise HandlerError(
                    "snyk scan required; aborting execution", terminate=True
                )

    def _raise_if_not_ready(self):
        """_raise_if_not_ready will raise a HandlerError if the handler is not ready

        Returns:
            None
        """
        if not self.is_ready():
            raise HandlerError(
                "Snyk handler is not ready to execute", terminate=self._required
            )

    @staticmethod
    def _snyk_runable(path):
        """_snyk_runable is a static method that checks if the snyk binary is runnable

        Parameters:
            path (str): the path to the snyk binary

        Returns:
            bool: True if the snyk binary is runnable, otherwise False
        """
        if not os.path.exists(path):
            return False
        if not os.access(path, os.X_OK):
            return False
        if os.environ["SNYK_TOKEN"] is None:
            log.warn("Environment variable SNYK_TOKEN not found")
            return False
        return True
