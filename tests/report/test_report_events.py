from datetime import datetime, timezone
from unittest import mock

import tfworker.report.events as events
from tfworker.report.plans import PlanManifest

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


class Sink:
    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    def send(self, event):
        if self.fail:
            raise RuntimeError("down")
        self.events.append(event)


def manifest():
    return PlanManifest(
        plan_id="P",
        run_id="R",
        deployment="dep",
        definition="def",
        plan_key="k",
        sha256="0" * 64,
        size=1,
        created_at=NOW,
    )


class TestEnvelope:
    def test_structured_cloudevent(self):
        event = events.cloudevent(
            events.PLAN_EVENT, "dep", "def", manifest(), run_id="R", time=NOW
        )
        assert event["specversion"] == "1.0"
        assert len(event["id"]) == 26
        assert event["source"] == "urn:tfworker:deployment:dep"
        assert event["type"] == "io.tfworker.plan.v1"
        assert event["subject"] == "def"
        assert event["time"] == "2026-11-27T12:00:00+00:00"
        assert event["datacontenttype"] == "application/json"
        assert event["tfworkerrunid"] == "R"
        assert event["data"]["schema"] == "tfworker.plan/v1"
        assert event["data"]["plan_id"] == "P"

    def test_optional_attributes_are_left_out(self):
        event = events.cloudevent(events.RUN_EVENT, "dep", None, {"a": 1})
        assert "subject" not in event
        assert "tfworkerrunid" not in event
        assert event["data"] == {"a": 1}
        assert event["time"].endswith("+00:00")


class TestPublish:
    def test_every_sink_gets_the_event(self):
        first, second = Sink(), Sink()
        events.subscribe(first)
        events.subscribe(second)
        events.subscribe(first)
        events.publish(events.RUN_EVENT, "dep", None, {"a": 1}, "R")
        assert len(first.events) == 1
        assert first.events[0]["id"] == second.events[0]["id"]

    def test_a_failing_sink_is_logged_and_skipped(self):
        bad, good = Sink(fail=True), Sink()
        events.subscribe(bad)
        events.subscribe(good)
        with mock.patch.object(events.log, "error") as error:
            events.publish(events.RUN_EVENT, "dep", None, {"a": 1})
        assert "down" in error.call_args[0][0]
        assert len(good.events) == 1

    def test_nothing_is_built_without_sinks(self):
        with mock.patch.object(events, "cloudevent") as build:
            events.publish(events.RUN_EVENT, "dep", None, {"a": 1})
        build.assert_not_called()
        assert events.subscribed() is False

    def test_an_event_that_cannot_be_built_is_logged(self):
        sink = Sink()
        events.subscribe(sink)
        with (
            mock.patch.object(events, "cloudevent", side_effect=ValueError("bad")),
            mock.patch.object(events.log, "error") as error,
        ):
            events.publish(events.RUN_EVENT, "dep", None, {"a": 1})
        assert "bad" in error.call_args[0][0]
        assert sink.events == []

    def test_unsubscribe(self):
        sink = Sink()
        events.subscribe(sink)
        events.unsubscribe(sink)
        events.unsubscribe(sink)
        events.publish(events.RUN_EVENT, "dep", None, {"a": 1})
        assert sink.events == []
