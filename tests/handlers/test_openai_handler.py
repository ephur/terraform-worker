import os
from unittest.mock import MagicMock, patch

import pytest

from tfworker.commands.terraform import TerraformResult
from tfworker.definitions import Definition
from tfworker.exceptions import HandlerError
from tfworker.handlers import OpenAIConfig, OpenAIHandler
from tfworker.types import TerraformAction, TerraformStage


@patch.dict(os.environ, {}, clear=True)
def test_init_missing_key():
    config = OpenAIConfig()
    with pytest.raises(HandlerError):
        OpenAIHandler(config)


@patch.dict(os.environ, {"OPENAI_API_KEY": "sk"}, clear=True)
@patch("openai.chat.completions.create")
def test_execute_summary_creates_file(mock_create, tmp_path):
    mock_resp = MagicMock()
    mock_message = MagicMock()
    mock_message.content = "summary"
    mock_resp.choices = [MagicMock(message=mock_message)]
    mock_create.return_value = mock_resp

    plan_file = tmp_path / "plan.tfplan"
    json_file = plan_file.with_suffix(".tfplan.json")
    json_file.write_text("{}")

    definition = Definition(name="def", path="path", plan_file=str(plan_file))
    result = TerraformResult(2, b"", b"")
    config = OpenAIConfig()
    handler = OpenAIHandler(config)
    handler.execute(
        TerraformAction.PLAN,
        TerraformStage.POST,
        "deploy",
        definition,
        str(tmp_path),
        result,
    )
    summary_file = json_file.with_suffix(config.summary_suffix)
    assert summary_file.exists()
    assert summary_file.read_text() == "summary"
