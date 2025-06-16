from unittest.mock import MagicMock, patch

import pytest

from tfworker.handlers.openai import (
    HandlerError,
    OpenAIConfig,
    OpenAIHandler,
    OpenAITask,
    OpenAITaskSettings,
    SummaryLevel,
)
from tfworker.types.terraform import TerraformAction, TerraformStage


class DummyDefinition:
    def __init__(self, plan_file):
        self.plan_file = plan_file


class DummyResult:
    def __init__(self, stdout="mock stdout", changed=True):
        self.stdout_str = stdout
        self._changed = changed

    def has_changes(self):
        return self._changed


class TestOpenAITaskMethods:
    def test_default_prompt_summary_levels(self):
        assert OpenAITask.SUMMARY.default_prompt(SummaryLevel.CONCISE).startswith(
            "You are a Terraform expert."
        )
        assert OpenAITask.SUMMARY.default_prompt(SummaryLevel.STANDARD).startswith(
            "You are a Terraform expert."
        )
        assert OpenAITask.SUMMARY.default_prompt(SummaryLevel.VERBOSE).startswith(
            "You are a Terraform expert."
        )

    def test_default_prompt_non_summary(self):
        assert "JSON" in OpenAITask.COST_PAYLOAD.default_prompt()
        assert "Estimate" in OpenAITask.COST_ESTIMATE.default_prompt()

    def test_default_suffix(self):
        assert OpenAITask.SUMMARY.default_suffix() == ".summary.md"
        assert OpenAITask.COST_ESTIMATE.default_suffix() == ".cost_estimate.txt"

    def test_log_header(self):
        assert OpenAITask.SUMMARY.log_header() == "OpenAI summary:"
        assert OpenAITask.COST_PAYLOAD.log_header() == "OpenAI cost payload:"
        assert OpenAITask.COST_ESTIMATE.log_header() == "OpenAI estimated cost:"


class TestOpenAIHandlerInit:
    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAPI_KEY", raising=False)
        with pytest.raises(HandlerError, match="OpenAI API key not found"):
            OpenAIHandler(OpenAIConfig())

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        handler = OpenAIHandler(OpenAIConfig())
        assert handler._ready


class TestOpenAIHandlerExecute:
    @pytest.fixture
    def handler(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        config = OpenAIConfig(
            tasks={
                OpenAITask.SUMMARY: OpenAITaskSettings(task_type=OpenAITask.SUMMARY)
            },
            send_plan_file=False,
        )
        return OpenAIHandler(config)

    @patch(
        "tfworker.handlers.openai.OpenAIHandler._invoke_openai",
        return_value="Mocked Output",
    )
    def test_execute_skips_if_no_changes(self, mock_ai, handler):
        definition = DummyDefinition("/tmp/mock.tfplan")
        result = DummyResult(changed=False)
        handler.execute(
            TerraformAction.PLAN, TerraformStage.POST, "demo", definition, ".", result
        )
        mock_ai.assert_not_called()

    @patch(
        "tfworker.handlers.openai.OpenAIHandler._invoke_openai", return_value="Output"
    )
    def test_execute_uses_stdout(self, mock_ai, handler, tmp_path):
        f = tmp_path / "plan.tfplan"
        f.write_text("original")
        definition = DummyDefinition(str(f))
        result = DummyResult(stdout="fake plan")

        handler.execute(
            TerraformAction.PLAN,
            TerraformStage.POST,
            "prodish",
            definition,
            str(tmp_path),
            result,
        )

        outfile = tmp_path / "plan.tfplan.summary.md"
        assert outfile.exists()
        assert outfile.read_text() == "Output"
        mock_ai.assert_called_once()

    def test_execute_missing_planfile(self, handler):
        with pytest.raises(HandlerError, match="planfile is not provided"):
            handler.execute(
                TerraformAction.PLAN,
                TerraformStage.POST,
                "stack",
                DummyDefinition(None),
                ".",
                DummyResult(),
            )

    def test_execute_wrong_stage_or_action(self, handler):
        definition = DummyDefinition("/tmp/dummy.tfplan")
        result = DummyResult()
        # These should all short-circuit
        handler.execute(
            TerraformAction.APPLY, TerraformStage.POST, "x", definition, ".", result
        )
        handler.execute(
            TerraformAction.PLAN, TerraformStage.PRE, "x", definition, ".", result
        )
        handler.execute(
            TerraformAction.PLAN, TerraformStage.POST, "x", definition, ".", None
        )  # no result


class TestOpenAIInvoke:
    @pytest.fixture
    def handler(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "xxx")
        return OpenAIHandler(OpenAIConfig())

    @patch("openai.chat.completions.create")
    def test_openai_success(self, mock_create, handler):
        mock_create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="Answer"))]
        )
        assert handler._invoke_openai("gpt-test", "prompt") == "Answer"

    @patch("openai.chat.completions.create", side_effect=Exception("Boom"))
    def test_openai_failure(self, mock_create, handler):
        with pytest.raises(HandlerError, match="OpenAI request failed: Boom"):
            handler._invoke_openai("model", "prompt")
