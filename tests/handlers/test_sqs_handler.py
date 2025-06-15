import json
from unittest.mock import MagicMock, patch

import boto3
from moto import mock_aws

from tfworker.commands.terraform import TerraformResult
from tfworker.definitions import Definition
from tfworker.handlers import QueueRule, SQSConfig, SQSHandler
from tfworker.types import TerraformAction, TerraformStage


class TestSQSHandlerTargetQueues:
    def test_basic_filters(self):
        config = SQSConfig(
            queues=["q"], actions=[TerraformAction.APPLY], stages=[TerraformStage.POST]
        )
        handler = SQSHandler(config)
        assert handler._target_queues(
            TerraformAction.APPLY, TerraformStage.POST, None
        ) == ["q"]
        assert (
            handler._target_queues(TerraformAction.PLAN, TerraformStage.PRE, None) == []
        )

    def test_result_filter(self):
        config = SQSConfig(queues=["q"], results=[0])
        handler = SQSHandler(config)
        ok = TerraformResult(0, b"ok", b"")
        fail = TerraformResult(1, b"fail", b"")
        assert handler._target_queues(
            TerraformAction.APPLY, TerraformStage.POST, ok
        ) == ["q"]
        assert (
            handler._target_queues(TerraformAction.APPLY, TerraformStage.POST, fail)
            == []
        )

    def test_advanced_rules(self):
        config = SQSConfig(
            queues={
                "q1": QueueRule(
                    actions=[TerraformAction.PLAN], stages=[TerraformStage.PRE]
                ),
                "q2": QueueRule(
                    actions=[TerraformAction.APPLY],
                    stages=[TerraformStage.POST],
                    results=[0],
                ),
            }
        )
        handler = SQSHandler(config)
        assert handler._target_queues(
            TerraformAction.PLAN, TerraformStage.PRE, None
        ) == ["q1"]
        ok = TerraformResult(0, b"ok", b"")
        assert handler._target_queues(
            TerraformAction.APPLY, TerraformStage.POST, ok
        ) == ["q2"]
        fail = TerraformResult(1, b"fail", b"")
        assert (
            handler._target_queues(TerraformAction.APPLY, TerraformStage.POST, fail)
            == []
        )


class TestSQSHandlerBuildMessage:
    def test_include_plan(self, tmp_path):
        plan_file = tmp_path / "plan.tfplan"
        plan_file.write_text("plan content")
        definition = Definition(name="def", path="path", plan_file=str(plan_file))
        result = TerraformResult(0, b"stdout", b"")
        config = SQSConfig(queues=["q"], include_plan=True)
        handler = SQSHandler(config)
        msg = json.loads(
            handler._build_message(
                TerraformAction.PLAN,
                TerraformStage.POST,
                "deploy",
                definition,
                str(tmp_path),
                result,
            )
        )
        assert msg["plan"] == "plan content"


@mock_aws
def test_execute_sends_message():
    session = boto3.Session()
    sqs = session.client("sqs", region_name="us-east-1")
    queue_url = sqs.create_queue(QueueName="test")["QueueUrl"]

    auths = {"aws": MagicMock(session=session)}
    app_state = MagicMock()
    app_state.authenticators = auths
    app_state.working_dir = "."
    ctx = MagicMock(obj=app_state)

    with patch("click.get_current_context", return_value=ctx):
        config = SQSConfig(queues=[queue_url])
        handler = SQSHandler(config)
        result = TerraformResult(0, b"stdout", b"")
        handler.execute(
            TerraformAction.APPLY,
            TerraformStage.POST,
            "deploy",
            Definition(name="def", path="path"),
            app_state.working_dir,
            result,
        )

    resp = sqs.receive_message(QueueUrl=queue_url)
    assert len(resp.get("Messages", [])) == 1
    body = json.loads(resp["Messages"][0]["Body"])
    assert body["definition"] == "def"
    assert body["exit_code"] == 0
