from datetime import datetime, timedelta, timezone

import pytest

from tfworker.backends.lock import LockInfo, WorkerLease

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def _lock(**overrides):
    data = {
        "ID": "abc",
        "Operation": "OperationTypePlan",
        "Who": "runner@pod-1",
        "Created": "2026-11-27T11:00:00.123456789Z",
    }
    data.update(overrides)
    return LockInfo.model_validate(data)


def _lease(**overrides):
    data = dict(
        definition="def1",
        action="apply",
        who="runner@pod-1",
        host="pod-1",
        pid=1,
        started_at=NOW - timedelta(minutes=5),
        heartbeat_at=NOW - timedelta(seconds=45),
        heartbeat_seconds=30,
    )
    data.update(overrides)
    return WorkerLease(**data)


class TestLockInfo:
    def test_parses_terraform_lock(self):
        lock = _lock()
        assert lock.id == "abc"
        assert lock.created.tzinfo is not None
        assert lock.age(NOW) == pytest.approx(3600 - 0.123456, abs=1e-3)

    def test_naive_created_is_utc(self):
        lock = _lock(Created="2026-11-27T11:00:00")
        assert lock.created.tzinfo == timezone.utc

    def test_ignores_unknown_fields(self):
        assert _lock(Extra="x").id == "abc"

    @pytest.mark.parametrize(
        "who,host", [("runner@pod-1", "pod-1"), ("pod-2", "pod-2"), ("", "")]
    )
    def test_host(self, who, host):
        assert _lock(Who=who).host == host

    def test_created_required(self):
        with pytest.raises(ValueError):
            LockInfo.model_validate({"ID": "abc"})


class TestWorkerLease:
    def test_fresh_within_two_intervals(self):
        assert _lease().is_fresh(NOW)
        assert not _lease(heartbeat_at=NOW - timedelta(seconds=61)).is_fresh(NOW)

    def test_naive_times_are_utc(self):
        lease = _lease(heartbeat_at=datetime(2026, 11, 27, 11, 59, 30))
        assert lease.heartbeat_at.tzinfo == timezone.utc
        assert lease.is_fresh(NOW)

    def test_holds_matching_host(self):
        assert _lease().holds(_lock())
        assert not _lease(host="pod-9").holds(_lock())
        assert not _lease(host="").holds(_lock(Who=""))
