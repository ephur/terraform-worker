"""OpenAI handler for post-plan analysis.

Example configuration, to enable a task without any custom settings, provide an empty dictionary: such as `summary: {}`.

```yaml
handlers:
  openai:
    tasks:
      summary:
        model: gpt-4o # gpt-3.5-turbo|gpt-4|gpt-4o|gpt-4o-mini|gpt-4.1|gpt-4.1-nano
        level: standard # terse|standard|verbose
        custom_prompt: |
          Summarize this plan focusing on security concerns.
      cost_payload:
        model: gpt-4o-mini
      cost_estimate:
        model: gpt-4.1-nano
    send_plan_file: false
```
"""

import os
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Union

import openai
from pydantic import BaseModel, Field, model_validator

import tfworker.util.log as log
from tfworker.exceptions import HandlerError
from tfworker.types.terraform import TerraformAction, TerraformStage

from .base import BaseHandler
from .registry import HandlerRegistry
from .results import BaseHandlerResult

if TYPE_CHECKING:  # pragma: no cover
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition


class OpenAITask(str, Enum):
    SUMMARY = "summary"
    COST_PAYLOAD = "cost_payload"
    COST_ESTIMATE = "cost_estimate"

    def default_prompt(self, level: "SummaryLevel" = None) -> str:
        if self == OpenAITask.SUMMARY:
            # level is required for SUMMARY
            if level is None:
                raise ValueError("SummaryLevel must be provided for SUMMARY task")
            if level == SummaryLevel.CONCISE:
                prefix = "You are a Terraform expert. Summarize the following Terraform plan, briefly recap the changes:"
            elif level == SummaryLevel.STANDARD:
                prefix = "You are a Terraform expert. Summarize the following Terraform plan in detail, include all changes and highlight potential risks:"
            else:
                prefix = "You are a Terraform expert. Summarize the following Terraform plan in exhaustive detail, highlight potential risks, resource misconfigurations, and anti-patterns:"
            return prefix
        elif self == OpenAITask.COST_PAYLOAD:
            return "Convert the following Terraform plan output into JSON suitable for the AWS Cost Calculator API.\nReturn only JSON."
        elif self == OpenAITask.COST_ESTIMATE:
            return "Estimate the monthly cost of the resources described in this Terraform plan. Provide a number in USD and explanation."
        else:
            return ""

    def default_suffix(self) -> str:
        if self == OpenAITask.SUMMARY:
            return ".summary.md"
        elif self == OpenAITask.COST_PAYLOAD:
            return ".cost_payload.json"
        elif self == OpenAITask.COST_ESTIMATE:
            return ".cost_estimate.txt"
        else:
            return f".{self.value}.txt"

    def log_header(self) -> str:
        if self == OpenAITask.SUMMARY:
            return "OpenAI summary:"
        elif self == OpenAITask.COST_PAYLOAD:
            return "OpenAI cost payload:"
        elif self == OpenAITask.COST_ESTIMATE:
            return "OpenAI estimated cost:"
        else:
            return f"OpenAI {self.value.replace('_', ' ')}:"


class OpenAIModel(str, Enum):
    # small token limits will make must outputs fail
    GPT35 = "gpt-3.5-turbo"
    GPT4 = "gpt-4"
    GPT4O = "gpt-4o"
    GPT4O_MINI = "gpt-4o-mini"
    GPT41 = "gpt-4.1"
    GPT41_NANO = "gpt-4.1-nano"


class SummaryLevel(str, Enum):
    CONCISE = "concise"
    STANDARD = "standard"
    VERBOSE = "verbose"


class OpenAITaskSettings(BaseModel):
    model: OpenAIModel = OpenAIModel.GPT41_NANO
    level: SummaryLevel = SummaryLevel.STANDARD
    custom_prompt: str | None = None
    task_type: OpenAITask

    model_config = {
        "extra": "forbid",
    }

    @property
    def prompt(self) -> str:
        if self.custom_prompt:
            return self.custom_prompt
        # Use the default_prompt method from the task_type enum
        if self.task_type == OpenAITask.SUMMARY:
            return self.task_type.default_prompt(self.level)
        else:
            return self.task_type.default_prompt()


class OpenAIConfig(BaseModel):
    tasks: Dict[OpenAITask, OpenAITaskSettings] = Field(
        default_factory=lambda: {
            OpenAITask.SUMMARY: OpenAITaskSettings(task_type=OpenAITask.SUMMARY),
        }
    )
    send_plan_file: bool = False
    required: bool = False

    model_config = {
        "extra": "forbid",
    }

    @model_validator(mode="before")
    @classmethod
    def validate_tasks(cls, values):
        tasks_raw = values.get("tasks", {})
        if not isinstance(tasks_raw, dict):
            return values
        tasks = {}
        for key, val in tasks_raw.items():
            try:
                task_key = OpenAITask(key)
            except ValueError:
                # skip unknown keys
                continue
            if val is None:
                val = {}
            elif not isinstance(val, dict):
                # if val is not dict, skip or treat as empty dict
                val = {}
            # Set task_type explicitly and filter unknown fields by using OpenAITaskSettings parsing
            task_settings = OpenAITaskSettings(task_type=task_key, **val)
            tasks[task_key] = task_settings
        # Replace tasks with parsed dict
        values["tasks"] = tasks
        return values

    @property
    def enabled_tasks(self) -> List[OpenAITask]:
        return list(self.tasks.keys())


class OpenAIResult(BaseHandlerResult):
    task: OpenAITask
    file: str
    content: str


@HandlerRegistry.register("openai")
class OpenAIHandler(BaseHandler):
    """Analyze terraform plans using OpenAI."""

    actions = [TerraformAction.PLAN]
    config_model = OpenAIConfig
    _ready = False
    default_priority = {
        TerraformAction.PLAN: 50,
    }

    def __init__(self, config: OpenAIConfig) -> None:
        for field in config.__class__.model_fields:
            setattr(self, f"_{field}", getattr(config, field))
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
    ) -> List[OpenAIResult] | None:  # pragma: no cover - entry point
        log.debug(
            f"OpenAIHandler.execute called with action={action}, stage={stage}, deployment={deployment}, working_dir={working_dir}"
        )
        if not (
            action == TerraformAction.PLAN
            and stage == TerraformStage.POST
            and result is not None
            and result.has_changes()
        ):
            return None

        planfile = definition.plan_file
        if planfile is None:
            raise HandlerError(
                "planfile is not provided, can't analyze", terminate=self._required
            )

        jsonfile = Path(planfile).with_suffix(".tfplan.json")
        if self._send_plan_file:
            if not jsonfile.exists():
                raise HandlerError(
                    f"plan json file not found: {jsonfile}", terminate=self._required
                )
            plan_text = jsonfile.read_text()
        else:
            plan_text = result.stdout_str

        results: List[OpenAIResult] = []
        for task, settings in self._tasks.items():
            prompt = f"{settings.prompt}\n{plan_text}"
            output = self._invoke_openai(settings.model.value, prompt)
            log.info(task.log_header())
            log.info(output)
            suffix = task.default_suffix()
            out_file = jsonfile.with_suffix(suffix)
            Path(out_file).write_text(output)
            log.debug(f"OpenAI {task.value} written to: {out_file}")
            results.append(
                OpenAIResult(
                    handler="openai",
                    action=action,
                    stage=stage,
                    task=task,
                    file=str(out_file),
                    content=output,
                )
            )
        return results

    def _invoke_openai(self, model: str, prompt: str) -> str:
        log.trace(f"Submitting prompt to OpenAI model={model}, prompt:\n{prompt}")
        try:
            resp = openai.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:  # pragma: no cover - network errors
            raise HandlerError(f"OpenAI request failed: {e}", terminate=self._required)
        return resp.choices[0].message.content.strip()
