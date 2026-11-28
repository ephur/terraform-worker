"""Models for terraform state locks and the worker's lease beside them."""

import re
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Go writes up to nine fractional digits; Python parses at most six
_FRACTION = re.compile(r"(\.\d{6})\d+")


def _parse_timestamp(value):
    if isinstance(value, str):
        value = _FRACTION.sub(r"\1", value.strip())
    return value


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class LockInfo(BaseModel):
    """
    The lock terraform writes while a command holds a state.

    The S3 backend stores this JSON as the body of `<key>.tflock` when
    `use_lockfile` is on, and as the `Info` attribute of the DynamoDB item
    otherwise.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str = Field(alias="ID")
    operation: str = Field("", alias="Operation")
    info: str = Field("", alias="Info")
    who: str = Field("", alias="Who")
    version: str = Field("", alias="Version")
    created: datetime = Field(alias="Created")
    path: str = Field("", alias="Path")

    @field_validator("created", mode="before")
    @classmethod
    def _trim_fraction(cls, value):
        return _parse_timestamp(value)

    @field_validator("created")
    @classmethod
    def _created_aware(cls, value: datetime) -> datetime:
        return _aware(value)

    @property
    def host(self) -> str:
        """The host part of `Who`, which is the pod name in Kubernetes."""
        return self.who.rsplit("@", 1)[-1]

    def age(self, now: datetime) -> float:
        """Seconds since the lock was taken."""
        return (now - self.created).total_seconds()


# a lease missing this many heartbeats, plus the skew allowance, is dead
FRESH_INTERVALS = 3
CLOCK_SKEW_SECONDS = 60.0


class WorkerLease(BaseModel):
    """
    A heartbeat the worker keeps beside the state while terraform runs.

    It lets a reader tell a long-running command from one whose process died
    and left its lock behind.
    """

    model_config = ConfigDict(extra="ignore")

    definition: str
    action: str
    who: str
    host: str
    pid: int
    run_id: Optional[str] = None
    started_at: datetime
    heartbeat_at: datetime
    heartbeat_seconds: float

    @field_validator("started_at", "heartbeat_at")
    @classmethod
    def _times_aware(cls, value: datetime) -> datetime:
        return _aware(value)

    def is_fresh(self, now: datetime) -> bool:
        """
        True while the lease was refreshed within the freshness window: three
        heartbeat intervals plus an allowance for clock skew between hosts.
        """
        age = (now - self.heartbeat_at).total_seconds()
        return age <= FRESH_INTERVALS * self.heartbeat_seconds + CLOCK_SKEW_SECONDS

    def holds(self, lock: LockInfo) -> bool:
        """
        True when this lease belongs to the process holding the lock: the
        same host, and refreshed since about when the lock was taken. A lease
        left behind by an earlier process on that host does not count.
        """
        if not lock.host or lock.host != self.host:
            return False
        last_beat = (lock.created - self.heartbeat_at).total_seconds()
        return last_beat <= self.heartbeat_seconds
