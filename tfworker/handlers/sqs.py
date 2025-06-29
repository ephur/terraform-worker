"""AWS SQS handler for Terraform worker.

Example basic configuration::

    handlers:
      - name: sqs
        options:
          queues:
            - https://sqs.us-east-1.amazonaws.com/123456789012/my-queue
          include_plan: true

Advanced configuration with per-queue rules::

    handlers:
      - name: sqs
        options:
          queues:
            https://sqs.us-east-1.amazonaws.com/123456789012/plan-queue:
              actions: [plan]
              stages: [post]
            https://sqs.us-east-1.amazonaws.com/123456789012/apply-queue:
              actions: [apply]
              results: [0]

"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Union

import click
from pydantic import BaseModel, Field

import tfworker.util.log as log
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.exceptions import HandlerError

from .base import BaseHandler
from .registry import HandlerRegistry

if TYPE_CHECKING:
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition


class QueueRule(BaseModel):
    actions: List[TerraformAction] | None = None
    stages: List[TerraformStage] | None = None
    results: List[int] | None = None


class SQSConfig(BaseModel):
    queues: List[str] | Dict[str, QueueRule]
    actions: List[TerraformAction] | None = None
    stages: List[TerraformStage] | None = None
    results: List[int] | None = None
    include_plan: bool = Field(
        default=False,
        description="Include plan file content when sending post plan messages",
    )


@HandlerRegistry.register("sqs")
class SQSHandler(BaseHandler):
    """Send execution details to AWS SQS."""

    actions = [
        TerraformAction.PLAN,
        TerraformAction.APPLY,
        TerraformAction.DESTROY,
        TerraformAction.INIT,
    ]
    config_model = SQSConfig
    _ready = False

    def __init__(self, config: SQSConfig) -> None:
        self.config = config
        self._app_state = None
        self._sqs_client = None
        self._ready = False
        self._validate_queues()

    @property
    def app_state(self):
        if self._app_state is None:
            self._app_state = click.get_current_context().obj
        return self._app_state

    @property
    def sqs_client(self):
        if self._sqs_client is None:
            aws = self.app_state.authenticators.get("aws")
            if aws is None:
                raise ValueError("AWS authenticator not available")
            self._sqs_client = aws.session.client("sqs")
        return self._sqs_client

    def is_ready(self) -> bool:
        if not self._ready:
            _ = self.sqs_client
            self._ready = True
        return self._ready

    def execute(
        self,
        action: "TerraformAction",
        stage: "TerraformStage",
        deployment: str,
        definition: "Definition",
        working_dir: str,
        result: Union["TerraformResult", None] = None,
    ) -> None:  # pragma: no cover
        queues = self._target_queues(action, stage, result)
        if not queues:
            return
        message = self._build_message(
            action, stage, deployment, definition, working_dir, result
        )
        for queue in queues:
            try:
                self.sqs_client.send_message(QueueUrl=queue, MessageBody=message)
            except Exception as e:  # pragma: no cover - boto3 errors not easy in tests
                log.error(f"Failed to send SQS message to {queue}: {e}")

    def _target_queues(
        self,
        action: TerraformAction,
        stage: TerraformStage,
        result: Union["TerraformResult", None],
    ) -> List[str]:
        queues: List[str] = []
        if isinstance(self.config.queues, list):
            actions = self.config.actions or list(TerraformAction)
            stages = self.config.stages or list(TerraformStage)
            res_filter = self.config.results
            if action in actions and stage in stages:
                if result is None:
                    if res_filter is None:
                        queues.extend(self.config.queues)
                else:
                    if res_filter is None or result.exit_code in res_filter:
                        queues.extend(self.config.queues)
            return queues

        for q, rule in self.config.queues.items():
            acts = rule.actions or self.config.actions or list(TerraformAction)
            stgs = rule.stages or self.config.stages or list(TerraformStage)
            res_filter = (
                rule.results if rule.results is not None else self.config.results
            )
            if action in acts and stage in stgs:
                if result is None:
                    if res_filter is None:
                        queues.append(q)
                else:
                    if res_filter is None or result.exit_code in res_filter:
                        queues.append(q)
        return queues

    def _build_message(
        self,
        action: TerraformAction,
        stage: TerraformStage,
        deployment: str,
        definition: "Definition",
        working_dir: str,
        result: Union["TerraformResult", None],
    ) -> str:
        payload = {
            "deployment": deployment,
            "definition": definition.name,
            "action": str(action),
            "phase": str(stage),
        }
        if result is not None:
            payload.update(
                {
                    "exit_code": result.exit_code,
                    "stdout": result.stdout.decode(),
                    "stderr": result.stderr.decode(),
                }
            )
        if (
            self.config.include_plan
            and action == TerraformAction.PLAN
            and stage == TerraformStage.POST
            and definition.plan_file
            and Path(definition.plan_file).exists()
        ):
            try:
                with open(definition.plan_file) as f:
                    payload["plan"] = f.read()
            except OSError as e:  # pragma: no cover
                log.error(f"Unable to read plan file {definition.plan_file}: {e}")

        # attach handler results if available
        try:
            handlers = self.app_state.handlers
            if handlers:
                res = handlers.get_results(action=action, stage=stage)
                if res:
                    payload["handler_results"] = [r.model_dump() for r in res]
        except Exception:
            pass
        return json.dumps(payload)

    def _queue_urls(self) -> List[str]:
        if isinstance(self.config.queues, list):
            return self.config.queues
        return list(self.config.queues.keys())

    def _validate_queues(self) -> None:
        try:
            existing = self.sqs_client.list_queues().get("QueueUrls", [])
        except Exception as e:
            raise HandlerError(f"Unable to list SQS queues: {e}")
        missing = [q for q in self._queue_urls() if q not in existing]
        if missing:
            raise HandlerError(f"SQS queues not found: {', '.join(missing)}")
