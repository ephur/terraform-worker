"""
Report events as CloudEvents.

Producers call `publish` with the same models they write to the backend.
Sinks, such as the sqs handler in `cloudevents` format, subscribe to receive
every event as a CloudEvents 1.0 structured JSON envelope. Publishing never
raises: the backend stays the source of truth, and a consumer that missed an
event reconciles from it.
"""

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Union

from pydantic import BaseModel

import tfworker.util.log as log

from .ulid import new_ulid

RUN_EVENT = "io.tfworker.run.v1"
STATUS_EVENT = "io.tfworker.status.v1"
PLAN_EVENT = "io.tfworker.plan.v1"
LOCK_WAITING_EVENT = "io.tfworker.lock.waiting"
EVENT_TYPES = (RUN_EVENT, STATUS_EVENT, PLAN_EVENT, LOCK_WAITING_EVENT)


class EventSink(Protocol):
    def send(self, event: Dict[str, Any]) -> None:
        """Deliver one event; raising is logged and never fails the run."""


_sinks: List[EventSink] = []
_lock = threading.Lock()


def subscribe(sink: EventSink) -> None:
    with _lock:
        if sink not in _sinks:
            _sinks.append(sink)


def unsubscribe(sink: EventSink) -> None:
    with _lock:
        if sink in _sinks:
            _sinks.remove(sink)


def subscribed() -> bool:
    with _lock:
        return bool(_sinks)


def reset() -> None:
    with _lock:
        _sinks.clear()


def source(deployment: str) -> str:
    return f"urn:tfworker:deployment:{deployment}"


def cloudevent(
    event_type: str,
    deployment: str,
    subject: Optional[str],
    data: Union[BaseModel, Dict[str, Any]],
    run_id: Optional[str] = None,
    time: Optional[datetime] = None,
) -> Dict[str, Any]:
    """A CloudEvents 1.0 envelope in structured JSON mode."""
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json", by_alias=True)
    event = {
        "specversion": "1.0",
        "id": new_ulid(),
        "source": source(deployment),
        "type": event_type,
        "time": (time or datetime.now(timezone.utc)).isoformat(),
        "datacontenttype": "application/json",
        "data": data,
    }
    if subject:
        event["subject"] = subject
    if run_id:
        event["tfworkerrunid"] = run_id
    return event


def publish(
    event_type: str,
    deployment: str,
    subject: Optional[str],
    data: Union[BaseModel, Dict[str, Any]],
    run_id: Optional[str] = None,
) -> None:
    """Send an event to every sink; a sink that fails is logged and skipped."""
    with _lock:
        sinks = list(_sinks)
    if not sinks:
        return
    try:
        event = cloudevent(event_type, deployment, subject, data, run_id)
    except Exception as e:
        log.error(f"unable to build {event_type} event: {e}")
        return
    for sink in sinks:
        try:
            sink.send(event)
        except Exception as e:
            log.error(f"unable to publish {event_type} event: {e}")
