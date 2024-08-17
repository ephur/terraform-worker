import os
from pathlib import Path
from typing import TYPE_CHECKING, Union

from pydantic import BaseModel

import tfworker.util.log as log
from tfworker.exceptions import HandlerError
from tfworker.types.terraform import TerraformAction, TerraformStage

from ..util.system import pipe_exec, strip_ansi
from .base import BaseHandler
from .registry import HandlerRegistry

if TYPE_CHECKING:
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition


class TrivyConfig(BaseModel):
    args: dict = {}
    cache_dir: str = "/tmp/trivy_cache"
    debug: bool = False
    exit_code: str = "1"
    format: str = None
    handler_debug: bool = False
    path: str = "/usr/bin/trivy"
    quiet: bool = True
    required: bool = False
    severity: str = "HIGH,CRITICAL"
    skip_dirs: list = ["**/examples"]
    template: str = (
        '\'ERRORS: {{ range . }}{{ range .Misconfigurations}}{{ .Severity }} - {{ .ID }} - {{ .AVDID }} - {{ .Title -}} - {{ .Description }} - {{ .Message }} - {{ .Resolution }} - {{ .PrimaryURL }} - {{ range .References }}{{ . }}{{ end }}{{ "\\n" }}{{ end }}{{ "\\n" }}{{ end }}{{ "\\n" }}\''
    )
    skip_planfile: bool = False
    skip_definition: bool = False
    stream_output: bool = True


@HandlerRegistry.register("trivy")
class TrivyHandler(BaseHandler):
    """
    The TrivyHandler will execute a trivy scan on a specified terraform plan file
    """

    actions = [TerraformAction.PLAN]
    config_model = TrivyConfig
    _ready = False

    def __init__(self, config: BaseModel) -> None:
        # configure the handler
        for k in config.model_fields:
            setattr(self, f"_{k}", getattr(config, k))

        # ensure trivy is runnable
        if not self._trivy_runable(self._path):
            if self.required:
                raise HandlerError(
                    f"Trivy is not runnable at {self._path}", terminate=True
                )
            raise HandlerError(f"Trivy is not runnable at {self._path}")

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
        """execute is called when a handler should trigger, if this is run post plan
        and there are changes, a scan will be executed

        Parameters:
            action (str): the action that triggered the handler (one of plan, clean, apply, destroy)
            stage (str): the stage of the action (one of pre, post)
            planfile (str): the path to the terraform plan file
            definition_path (pathlib.Path): the path to the terraform definition
            changes (bool): True if there are changes, otherwise False
            kwargs: any additional arguments that may be passed (and ignored)

        Returns:
            None
        """
        # pre plan; trivy scan the definition if its applicable
        definition_path = definition.get_target_path(working_dir=working_dir)
        if action == TerraformAction.PLAN and stage == TerraformStage.PRE:
            if definition_path is None:
                raise HandlerError(
                    "definition_path is not provided, can't scan",
                    terminate=self._required,
                )

            if self._skip_definition:
                log.info(f"Skipping trivy scan of definition: {definition_path}")
                return None

            log.info(f"scanning definition with trivy: {definition_path}")
            self._scan(definition_path)

        # post plan; trivy scan the planfile if its applicable
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

            if definition_path is None:
                raise HandlerError(
                    "definition_path is not provided, can't scan",
                    terminate=self._required,
                )

            if self._skip_planfile:
                log.info(f"Skipping trivy scan of planfile: {planfile}")
                return None

            log.info(f"scanning planfile with trivy: {planfile}")
            self._scan(definition_path, planfile)

    def _scan(self, definition_path: Path, planfile: Path = None):
        """_scan will execute a trivy scan on the provided planfile

        Parameters:
            definition_path (pathlib.Path): the path to the terraform definition
            planfile (str): the path to the terraform plan file

        Returns:
            None
        """
        # The ordering of items added to the list is important, don't change it without careful consideration
        self._raise_if_not_ready()

        trivy_args = []
        trivy_args.append(self._path)

        if self._quiet:
            trivy_args.append("--quiet")

        if self._debug:
            trivy_args.append("--debug")

        if planfile is None:
            trivy_args.append("fs")
            trivy_args.append("--scanners")
            trivy_args.append("misconfig,secret")
            if len(self._skip_dirs) > 0:
                trivy_args.append("--skip-dirs")
                trivy_args.append(",".join(self._skip_dirs))
        else:
            trivy_args.append("config")

        trivy_args.append("--cache-dir")
        trivy_args.append(self._cache_dir)
        trivy_args.append("--severity")
        trivy_args.append(self._severity)
        trivy_args.append("--exit-code")
        trivy_args.append(self._exit_code)

        if self._format:
            trivy_args.append("--format")
            trivy_args.append(self._format)

            if self._format == "template":
                trivy_args.append("--template")
                trivy_args.append(self._template)

        for k, v in self._args.items():
            trivy_args.append(f"--{k}")
            trivy_args.append(v)

        if planfile is not None:
            trivy_args.append(str(Path.resolve(planfile)))
        else:
            trivy_args.append(".")

        try:
            if self._debug:
                log.debug(f"cmd: {' '.join(trivy_args)}")
            (exit_code, stdout, stderr) = pipe_exec(
                f"{' '.join(trivy_args)}",
                cwd=str(definition_path),
                stream_output=self._stream_output,
            )
        except Exception as e:
            raise HandlerError(f"Error executing trivy scan: {e}")

        self._handle_results(exit_code, stdout, stderr, planfile)

    def _handle_results(self, exit_code, stdout, stderr, planfile):
        """_handle_results will handle the results of the trivy scan

        Parameters:
            exit_code (int): the exit code of the trivy scan
            stdout (str): the stdout of the trivy scan
            stderr (str): the stderr of the trivy scan

        Returns:
            None
        """
        if exit_code != 0:
            log.error(f"trivy scan failed with exit code {exit_code}")
            if self._stream_output is False:
                log.error(strip_ansi(f"stdout: {stdout.decode('UTF-8')}"))
                log.error(strip_ansi(f"stderr: {stderr.decode('UTF-8')}"))

            if self._required:
                if planfile is not None:
                    log.warn(f"Removing planfile: {planfile}")
                    os.remove(planfile)
                raise HandlerError(
                    "trivy scan required; aborting execution", terminate=True
                )

    def _raise_if_not_ready(self):
        """_raise_if_not_ready will raise a HandlerError if the handler is not ready

        Returns:
            None
        """
        if not self.is_ready():
            raise HandlerError(
                "Trivy handler is not ready to execute", terminate=self._required
            )

    @staticmethod
    def _trivy_runable(path):
        """_trivy_runable is a static method that checks if the trivy binary is runnable

        Parameters:
            path (str): the path to the trivy binary

        Returns:
            bool: True if the trivy binary is runnable, otherwise False
        """

        if not os.path.exists(path):
            return False
        if not os.access(path, os.X_OK):
            return False
        return True
