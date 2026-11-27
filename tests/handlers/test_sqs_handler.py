import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

import tfworker.report.events as events
from tfworker.commands.terraform import TerraformResult
from tfworker.custom_types import TerraformAction, TerraformStage
from tfworker.definitions import Definition
from tfworker.exceptions import HandlerError
from tfworker.handlers import QueueRule, SQSConfig, SQSHandler


class TestSQSHandlerTargetQueues:
    @patch("tfworker.handlers.sqs.SQSHandler._validate_queues", return_value=None)
    def test_basic_filters(self, _mock_validate):
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

    @patch("tfworker.handlers.sqs.SQSHandler._validate_queues", return_value=None)
    def test_default_stages_exclude_error(self, _mock_validate):
        """With no explicit stages, the ERROR stage must not target queues."""
        config = SQSConfig(queues=["q"])
        handler = SQSHandler(config)
        assert (
            handler._target_queues(TerraformAction.APPLY, TerraformStage.ERROR, None)
            == []
        )
        # PRE/POST still default-on
        assert handler._target_queues(
            TerraformAction.APPLY, TerraformStage.POST, None
        ) == ["q"]

    @patch("tfworker.handlers.sqs.SQSHandler._validate_queues", return_value=None)
    def test_result_filter(self, _mock_validate):
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

    @patch("tfworker.handlers.sqs.SQSHandler._validate_queues", return_value=None)
    def test_advanced_rules(self, _mock_validate):
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
    @patch("tfworker.handlers.sqs.SQSHandler._validate_queues", return_value=None)
    def test_include_plan(self, _mock_validate, tmp_path):
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


class TestSQSHandlerIsReady:
    @mock_aws
    def test_validates_queues(self):
        session = boto3.Session()
        sqs = session.client("sqs", region_name="us-east-1")
        queue_url = sqs.create_queue(QueueName="test")["QueueUrl"]

        auths = {"aws": MagicMock(session=session)}
        app_state = MagicMock()
        app_state.authenticators = auths
        ctx = MagicMock(obj=app_state)

        with patch("click.get_current_context", return_value=ctx):
            config = SQSConfig(queues=[queue_url])
            handler = SQSHandler(config)
            assert handler.is_ready() is True

    @mock_aws
    def test_missing_queue(self):
        session = boto3.Session()
        sqs = session.client("sqs", region_name="us-east-1")
        existing_url = sqs.create_queue(QueueName="test")["QueueUrl"]

        auths = {"aws": MagicMock(session=session)}
        app_state = MagicMock()
        app_state.authenticators = auths
        ctx = MagicMock(obj=app_state)

        with patch("click.get_current_context", return_value=ctx):
            config = SQSConfig(
                queues=[
                    existing_url,
                    "https://sqs.us-east-1.amazonaws.com/123456789012/missing",
                ]
            )
            with pytest.raises(HandlerError):
                SQSHandler(config)


class TestSQSHandlerExecute:
    @mock_aws
    def test_execute_sends_message(self):
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


class TestSQSConfigFormats:
    def test_legacy_allows_empty_queues(self):
        assert SQSConfig(queues=[]).queues == []
        assert SQSConfig(queues={}).queues == {}

    def test_empty_types_are_rejected(self):
        with pytest.raises(ValueError):
            SQSConfig(format="cloudevents", event_bus="bus", types=[])
        with pytest.raises(ValueError):
            QueueRule(types=[])

    def test_legacy_rejects_cloudevents_options(self):
        with pytest.raises(ValueError, match="format: cloudevents"):
            SQSConfig(queues=["q"], event_bus="bus")

    def test_legacy_rejects_an_empty_bus(self):
        with pytest.raises(ValueError, match="format: cloudevents"):
            SQSConfig(queues=["q"], event_bus="")

    def test_cloudevents_needs_a_target(self):
        with pytest.raises(ValueError, match="queues, an event_bus"):
            SQSConfig(format="cloudevents")

    def test_cloudevents_with_only_a_bus(self):
        assert SQSConfig(format="cloudevents", event_bus="bus").queues == []


def _event(event_type="io.tfworker.run.v1"):
    return {"specversion": "1.0", "id": "1", "type": event_type, "data": {}}


@pytest.fixture
def aws_events():
    with mock_aws():
        session = boto3.Session(region_name="us-east-1")
        sqs = session.client("sqs")
        bridge = session.client("events")
        app_state = MagicMock()
        app_state.authenticators = {"aws": MagicMock(session=session)}
        ctx = MagicMock(obj=app_state)
        with patch("click.get_current_context", return_value=ctx):
            yield sqs, bridge


def _messages(sqs, url):
    found = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10)
    return [json.loads(m["Body"]) for m in found.get("Messages", [])]


class TestSQSHandlerCloudEvents:
    def test_subscribes_and_publishes_to_every_queue(self, aws_events):
        sqs, _ = aws_events
        url = sqs.create_queue(QueueName="all")["QueueUrl"]
        SQSHandler(SQSConfig(format="cloudevents", queues=[url]))
        events.publish(events.RUN_EVENT, "dep", None, {"a": 1}, "R")
        (body,) = _messages(sqs, url)
        assert body["type"] == "io.tfworker.run.v1"
        assert body["data"] == {"a": 1}
        assert body["tfworkerrunid"] == "R"

    def test_legacy_format_does_not_subscribe(self, aws_events):
        sqs, _ = aws_events
        url = sqs.create_queue(QueueName="legacy")["QueueUrl"]
        SQSHandler(SQSConfig(queues=[url]))
        assert events.subscribed() is False

    def test_cloudevents_format_sends_no_stage_messages(self, aws_events):
        sqs, _ = aws_events
        url = sqs.create_queue(QueueName="quiet")["QueueUrl"]
        handler = SQSHandler(SQSConfig(format="cloudevents", queues=[url]))
        handler.execute(
            TerraformAction.APPLY,
            TerraformStage.POST,
            "dep",
            Definition(name="def", path="path"),
            ".",
            TerraformResult(0, b"", b""),
        )
        assert _messages(sqs, url) == []

    def test_routing_by_type(self, aws_events):
        sqs, _ = aws_events
        plans = sqs.create_queue(QueueName="plans")["QueueUrl"]
        runs = sqs.create_queue(QueueName="runs")["QueueUrl"]
        handler = SQSHandler(
            SQSConfig(
                format="cloudevents",
                queues={plans: QueueRule(types=["io.tfworker.plan.*"]), runs: {}},
                types=["io.tfworker.run.v1"],
            )
        )
        handler.send(_event("io.tfworker.plan.v1"))
        handler.send(_event("io.tfworker.run.v1"))
        handler.send(_event("io.tfworker.lock.waiting"))
        assert [m["type"] for m in _messages(sqs, plans)] == ["io.tfworker.plan.v1"]
        assert [m["type"] for m in _messages(sqs, runs)] == ["io.tfworker.run.v1"]

    def test_list_queues_filtered_by_type(self, aws_events):
        sqs, _ = aws_events
        url = sqs.create_queue(QueueName="filtered")["QueueUrl"]
        handler = SQSHandler(
            SQSConfig(format="cloudevents", queues=[url], types=["io.tfworker.lock.*"])
        )
        handler.send(_event("io.tfworker.run.v1"))
        handler.send(_event("io.tfworker.lock.waiting"))
        assert [m["type"] for m in _messages(sqs, url)] == ["io.tfworker.lock.waiting"]

    def test_queue_failure_is_logged(self, aws_events):
        sqs, _ = aws_events
        url = sqs.create_queue(QueueName="gone")["QueueUrl"]
        handler = SQSHandler(SQSConfig(format="cloudevents", queues=[url]))
        sqs.delete_queue(QueueUrl=url)
        with patch("tfworker.handlers.sqs.log.error") as error:
            handler.send(_event())
        assert "Failed to send io.tfworker.run.v1 event" in error.call_args[0][0]

    def test_event_bridge(self, aws_events):
        _, bridge = aws_events
        bridge.create_event_bus(Name="tfworker")
        handler = SQSHandler(
            SQSConfig(
                format="cloudevents",
                event_bus="tfworker",
                types=["io.tfworker.status.*"],
            )
        )
        with patch.object(
            handler.events_client,
            "put_events",
            wraps=handler.events_client.put_events,
        ) as put:
            handler.send(_event("io.tfworker.status.v1"))
            handler.send(_event("io.tfworker.run.v1"))
        (entry,) = put.call_args.kwargs["Entries"]
        assert put.call_count == 1
        assert entry["Source"] == "tfworker"
        assert entry["DetailType"] == "io.tfworker.status.v1"
        assert entry["EventBusName"] == "tfworker"
        assert json.loads(entry["Detail"])["type"] == "io.tfworker.status.v1"

    def test_missing_event_bus(self, aws_events):
        with pytest.raises(HandlerError, match="EventBridge bus nope"):
            SQSHandler(SQSConfig(format="cloudevents", event_bus="nope"))

    def test_rejected_entry_is_logged(self, aws_events):
        _, bridge = aws_events
        bridge.create_event_bus(Name="tfworker")
        handler = SQSHandler(SQSConfig(format="cloudevents", event_bus="tfworker"))
        rejected = {
            "FailedEntryCount": 1,
            "Entries": [{"ErrorCode": "Throttled", "ErrorMessage": "slow down"}],
        }
        with (
            patch.object(handler.events_client, "put_events", return_value=rejected),
            patch("tfworker.handlers.sqs.log.error") as error,
        ):
            handler.send(_event())
        assert "Throttled slow down" in error.call_args[0][0]

    def test_put_failure_is_logged(self, aws_events):
        _, bridge = aws_events
        bridge.create_event_bus(Name="tfworker")
        handler = SQSHandler(SQSConfig(format="cloudevents", event_bus="tfworker"))
        with (
            patch.object(
                handler.events_client, "put_events", side_effect=RuntimeError("down")
            ),
            patch("tfworker.handlers.sqs.log.error") as error,
        ):
            handler.send(_event())
        assert "down" in error.call_args[0][0]

    def test_events_client_needs_aws(self):
        handler = SQSHandler.__new__(SQSHandler)
        handler._events_client = None
        handler._app_state = MagicMock(authenticators={})
        with pytest.raises(ValueError, match="AWS authenticator"):
            _ = handler.events_client


class TestCloudEventsDelivery:
    def test_content_type_and_timeouts(self, aws_events):
        sqs, _ = aws_events
        url = sqs.create_queue(QueueName="typed")["QueueUrl"]
        handler = SQSHandler(SQSConfig(format="cloudevents", queues=[url]))
        handler.send(_event())
        found = sqs.receive_message(QueueUrl=url, MessageAttributeNames=["All"])
        attributes = found["Messages"][0]["MessageAttributes"]
        assert attributes["content-type"]["StringValue"] == (
            "application/cloudevents+json"
        )
        config = handler.sqs_client.meta.config
        assert config.connect_timeout == 5
        assert config.retries["total_max_attempts"] == 2

    def test_legacy_clients_keep_boto_defaults(self):
        handler = SQSHandler.__new__(SQSHandler)
        handler.config = SQSConfig(queues=["q"])
        assert handler._client_config() is None

    def test_a_failing_queue_does_not_stop_the_others(self, aws_events):
        sqs, bridge = aws_events
        bridge.create_event_bus(Name="tfworker")
        gone = sqs.create_queue(QueueName="gone2")["QueueUrl"]
        kept = sqs.create_queue(QueueName="kept")["QueueUrl"]
        handler = SQSHandler(
            SQSConfig(format="cloudevents", queues=[gone, kept], event_bus="tfworker")
        )
        sqs.delete_queue(QueueUrl=gone)
        with (
            patch.object(
                handler.events_client,
                "put_events",
                wraps=handler.events_client.put_events,
            ) as put,
            patch("tfworker.handlers.sqs.log.error"),
        ):
            handler.send(_event())
        assert len(_messages(sqs, kept)) == 1
        put.assert_called_once()
