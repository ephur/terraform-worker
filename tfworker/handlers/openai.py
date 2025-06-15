import os
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, List, Union

import openai
from pydantic import BaseModel, Field

import tfworker.util.log as log
from tfworker.exceptions import HandlerError
from tfworker.types.terraform import TerraformAction, TerraformStage

from .base import BaseHandler
from .registry import HandlerRegistry

if TYPE_CHECKING:  # pragma: no cover
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition


class OpenAITask(str, Enum):
    SUMMARY = "summary"
    COST_PAYLOAD = "cost_payload"
    COST_ESTIMATE = "cost_estimate"


class OpenAIModel(str, Enum):
    # small token limits will make must outputs fail
    GPT35 = "gpt-3.5-turbo"
    GPT4 = "gpt-4"
    GPT4O = "gpt-4o"


class OpenAIConfig(BaseModel):
    tasks: List[OpenAITask] = Field(default_factory=lambda: [OpenAITask.SUMMARY])
    summary_model: OpenAIModel = OpenAIModel.GPT4O
    cost_payload_model: OpenAIModel = OpenAIModel.GPT4O
    cost_estimate_model: OpenAIModel = OpenAIModel.GPT4O
    summary_suffix: str = ".summary.md"
    required: bool = False


@HandlerRegistry.register("openai")
class OpenAIHandler(BaseHandler):
    """Analyze terraform plans using OpenAI."""

    actions = [TerraformAction.PLAN]
    config_model = OpenAIConfig
    _ready = False

    def __init__(self, config: OpenAIConfig) -> None:
        for k in config.model_fields:
            setattr(self, f"_{k}", getattr(config, k))
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAPI_KEY")
        if not api_key:
            raise HandlerError(
                "OpenAI API key not found in environment (OPENAI_API_KEY or OPENAPI_KEY)",
                terminate=self._required,
            )
        openai.api_key = api_key
        self._ready = True

    def execute(
        self,
        action: "TerraformAction",
        stage: "TerraformStage",
        deployment: str,
        definition: "Definition",
        working_dir: str,
        result: Union["TerraformResult", None] = None,
    ) -> None:  # pragma: no cover - entry point
        if not (
            action == TerraformAction.PLAN
            and stage == TerraformStage.POST
            and result is not None
            and result.has_changes()
        ):
            return

        planfile = definition.plan_file
        if planfile is None:
            raise HandlerError(
                "planfile is not provided, can't analyze", terminate=self._required
            )

        jsonfile = Path(planfile).with_suffix(".tfplan.json")
        if not jsonfile.exists():
            raise HandlerError(
                f"plan json file not found: {jsonfile}", terminate=self._required
            )

        plan_text = jsonfile.read_text()

        if OpenAITask.SUMMARY in self._tasks:
            summary = self._summary(plan_text)
            log.info(summary)
            summary_file = jsonfile.with_suffix(self._summary_suffix)
            Path(summary_file).write_text(summary)

        if OpenAITask.COST_PAYLOAD in self._tasks:
            payload = self._cost_payload(plan_text)
            payload_file = jsonfile.with_suffix(".cost_payload.json")
            Path(payload_file).write_text(payload)

        if OpenAITask.COST_ESTIMATE in self._tasks:
            est = self._cost_estimate(plan_text)
            log.info(f"Estimated cost: {est}")
            est_file = jsonfile.with_suffix(".cost_estimate.txt")
            Path(est_file).write_text(est)

    # internal helpers -----------------------------------------------------
    def _summary(self, plan: str) -> str:
        prompt = (
            "Summarize the following Terraform plan. Highlight anti-patterns or potential risks:\n"
            + plan
        )
        return self._invoke_openai(self._summary_model.value, prompt)

    def _cost_payload(self, plan: str) -> str:
        prompt = (
            "Convert the following Terraform plan output into JSON suitable for the AWS Cost Calculator API."
            "\nReturn only JSON.\n" + plan
        )
        return self._invoke_openai(self._cost_payload_model.value, prompt)

    def _cost_estimate(self, plan: str) -> str:
        prompt = (
            "Estimate the monthly cost of the resources described in this Terraform plan."
            " Provide a number in USD and explanation.\n" + plan
        )
        return self._invoke_openai(self._cost_estimate_model.value, prompt)

    def _invoke_openai(self, model: str, prompt: str) -> str:
        try:
            resp = openai.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:  # pragma: no cover - network errors
            raise HandlerError(f"OpenAI request failed: {e}", terminate=self._required)
        return resp.choices[0].message.content.strip()
