from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from tfworker.backends.lock import LockInfo, WorkerLease
from tfworker.custom_types.config_file import ConfigFile, LockingOptions, OnStale
from tfworker.custom_types.terraform import TerraformAction
from tfworker.exceptions import BackendError, LockUnavailableError
from tfworker.locking import StateLocker, _Heartbeat

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
PLAN = TerraformAction.PLAN
APPLY = TerraformAction.APPLY
TARGET = "/work/definitions/def1"


def _lock(age=60, who="runner@pod-1"):
    return LockInfo.model_validate(
        {
            "ID": "lock-1",
            "Operation": "OperationTypeApply",
            "Who": who,
            "Created": (NOW - timedelta(seconds=age)).isoformat(),
        }
    )


def _lease(host="pod-1", heartbeat_age=10):
    return WorkerLease(
        definition="def1",
        action="apply",
        who=f"runner@{host}",
        host=host,
        pid=1,
        started_at=NOW - timedelta(hours=3),
        heartbeat_at=NOW - timedelta(seconds=heartbeat_age),
        heartbeat_seconds=30,
    )


class Clock:
    def __init__(self):
        self.mono = 1000.0
        self.sleeps = []

    def now(self):
        return NOW + timedelta(seconds=self.mono - 1000.0)

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.mono += seconds


@pytest.fixture
def backend():
    backend = MagicMock()
    backend.lock_inspection = True
    backend.get_lock.return_value = None
    backend.get_lease.return_value = None
    return backend


def make_locker(backend, clock=None, started=None, **options):
    locker = StateLocker(
        LockingOptions(**options),
        backend,
        "/usr/bin/terraform",
        env=lambda: {"A": "1"},
        run_id="run-7",
        deployment="dep",
    )
    clock = clock or Clock()
    locker._now = clock.now
    locker._monotonic = clock.monotonic
    locker._sleep = clock.sleep
    locker._host = "pod-9"
    if started is not None:
        locker._start_heartbeat = lambda action, name: started.append((action, name))
    return locker


def acquire(locker, action=PLAN):
    return locker.acquire(action, "def1", TARGET)


class TestLockingOptions:
    def test_off_unless_configured(self):
        assert ConfigFile().locking is None
        assert ConfigFile(locking={}).locking == LockingOptions()

    def test_frozen_with_the_config(self):
        config = ConfigFile(locking={"wait": "5m"}).freeze()
        with pytest.raises(Exception):
            config.locking.wait = 1

    def test_heartbeat_off_or_at_least_minimum(self):
        assert LockingOptions(heartbeat=0).heartbeat == 0
        assert LockingOptions(heartbeat="5s").heartbeat == 5

    def test_defaults(self):
        options = LockingOptions()
        assert options.wait == 600
        assert options.on_stale == OnStale.FAIL
        assert options.actions == [PLAN, APPLY, TerraformAction.DESTROY]

    def test_durations_and_single_action(self):
        options = LockingOptions(wait="1h", poll_interval="30s", actions="apply")
        assert options.wait == 3600
        assert options.poll_interval == 30
        assert options.actions == [APPLY]

    @pytest.mark.parametrize(
        "field,value",
        [
            ("poll_interval", 0),
            ("poll_interval", "0s"),
            ("on_timeout", "ignore"),
            ("wait", "soon"),
            ("on_stale", "ignore"),
            ("actions", ["init"]),
            ("heartbeat", "1s"),
            ("heartbeat", 0.001),
            ("unknown", 1),
        ],
    )
    def test_invalid(self, field, value):
        with pytest.raises(ValueError):
            LockingOptions(**{field: value})


class TestPolicy:
    def test_covers_configured_actions(self, backend):
        locker = make_locker(backend, actions=["apply"])
        assert locker.covers(APPLY)
        assert not locker.covers(PLAN)

    def test_fail_run(self, backend):
        assert not make_locker(backend).fail_run
        assert make_locker(backend, on_timeout="fail_run").fail_run

    def test_backend_without_inspection_only_sets_deadline(self, backend):
        backend.lock_inspection = False
        started = []
        locker = make_locker(backend, started=started, wait=90)
        assert acquire(locker) == 1090
        assert acquire(locker) == 1090
        backend.get_lock.assert_not_called()
        assert started == []
        assert locker._warned_no_inspection


class TestAcquire:
    def test_free_lock_proceeds_with_full_timeout(self, backend):
        started = []
        locker = make_locker(backend, started=started, wait=300)
        assert acquire(locker) == 1300
        assert started == [(PLAN, "def1")]

    def test_waits_for_live_lock(self, backend):
        backend.get_lock.side_effect = [_lock(), _lock(), None]
        clock = Clock()
        started = []
        locker = make_locker(
            backend, clock=clock, started=started, wait=600, poll_interval=10
        )
        assert acquire(locker, APPLY) == 1600
        assert len(clock.sleeps) == 2
        assert 8 <= clock.sleeps[0] <= 12
        assert 16 <= clock.sleeps[1] <= 24
        assert started == [(APPLY, "def1")]

    def test_backoff_is_capped(self, backend):
        locker = make_locker(backend, poll_interval=10)
        assert 32 <= locker._poll_delay(5) <= 48

    def test_times_out(self, backend):
        backend.get_lock.return_value = _lock()
        clock = Clock()
        locker = make_locker(backend, clock=clock, wait=60, poll_interval=15)
        with pytest.raises(
            LockUnavailableError, match="still locked after waiting 1m00s"
        ) as error:
            acquire(locker)
        assert sum(clock.sleeps) == pytest.approx(60)
        assert error.value.fail_run is False

    def test_on_timeout_fail_run(self, backend):
        backend.get_lock.return_value = _lock()
        locker = make_locker(backend, wait=0, on_timeout="fail_run")
        with pytest.raises(LockUnavailableError) as error:
            acquire(locker)
        assert error.value.fail_run is True

    def test_zero_wait_fails_at_once(self, backend):
        backend.get_lock.return_value = _lock()
        clock = Clock()
        with pytest.raises(LockUnavailableError, match="ID lock-1"):
            acquire(make_locker(backend, clock=clock, wait=0))
        assert clock.sleeps == []

    def test_unreadable_lock_is_left_to_terraform(self, backend):
        backend.get_lock.side_effect = BackendError("denied")
        started = []
        acquire(make_locker(backend, started=started))
        assert started


class TestStaleLocks:
    def test_old_lock_without_lease_fails(self, backend):
        backend.get_lock.return_value = _lock(age=3 * 3600)
        with pytest.raises(
            LockUnavailableError, match="stale lock .*force-unlock lock-1"
        ):
            acquire(make_locker(backend))

    def test_old_lock_with_fresh_lease_is_live(self, backend):
        backend.get_lock.side_effect = [_lock(age=3 * 3600), None]
        backend.get_lease.return_value = _lease()
        started = []
        acquire(make_locker(backend, started=started))
        assert started

    @pytest.mark.parametrize(
        "lease", [_lease(heartbeat_age=600), _lease(host="other-pod")]
    )
    def test_dead_or_foreign_lease_is_stale(self, backend, lease):
        backend.get_lock.return_value = _lock(age=3 * 3600)
        backend.get_lease.return_value = lease
        with pytest.raises(LockUnavailableError, match="stale"):
            acquire(make_locker(backend))

    def test_young_lock_with_dead_lease_is_stale(self, backend):
        backend.get_lock.return_value = _lock(age=300)
        backend.get_lease.return_value = _lease(heartbeat_age=200)
        with pytest.raises(LockUnavailableError, match="stale"):
            acquire(make_locker(backend))

    def test_lease_left_before_the_lock_is_ignored(self, backend):
        backend.get_lock.side_effect = [_lock(age=60), None]
        backend.get_lease.return_value = _lease(heartbeat_age=3600)
        started = []
        acquire(make_locker(backend, started=started))
        assert started

    def test_unreadable_lease_is_treated_as_live(self, backend):
        backend.get_lock.side_effect = [_lock(age=3 * 3600), None]
        backend.get_lease.side_effect = BackendError("denied")
        started = []
        acquire(make_locker(backend, started=started))
        assert started

    def test_on_stale_wait(self, backend):
        backend.get_lock.side_effect = [_lock(age=3 * 3600), None]
        clock = Clock()
        started = []
        acquire(make_locker(backend, clock=clock, started=started, on_stale="wait"))
        assert len(clock.sleeps) == 1
        assert started

    def test_on_stale_force_unlock(self, backend, mocker):
        # the lock must look stale on two polls before it is removed
        backend.get_lock.side_effect = [_lock(age=3 * 3600), _lock(age=3 * 3600), None]
        pipe = mocker.patch("tfworker.locking.pipe_exec", return_value=(0, b"", b""))
        started = []
        acquire(make_locker(backend, started=started, on_stale="force_unlock"))
        pipe.assert_called_once_with(
            "/usr/bin/terraform force-unlock -force lock-1",
            cwd=TARGET,
            env={"A": "1"},
        )
        assert started

    def test_force_unlock_once_per_lock(self, backend, mocker):
        backend.get_lock.return_value = _lock(age=3 * 3600)
        pipe = mocker.patch("tfworker.locking.pipe_exec", return_value=(0, b"", b""))
        locker = make_locker(backend, on_stale="force_unlock", wait=120)
        with pytest.raises(LockUnavailableError, match="still locked"):
            acquire(locker)
        pipe.assert_called_once()

    def test_lock_without_host_uses_age(self, backend):
        backend.get_lock.return_value = _lock(age=3 * 3600, who="")
        with pytest.raises(LockUnavailableError, match="stale"):
            acquire(make_locker(backend))
        backend.get_lease.assert_not_called()

    def test_lease_is_read_for_the_lock_host(self, backend):
        backend.get_lock.side_effect = [_lock(age=3 * 3600), None]
        backend.get_lease.return_value = _lease()
        acquire(make_locker(backend, started=[]))
        backend.get_lease.assert_called_once_with("def1", "pod-1")

    def test_force_unlock_failure(self, backend, mocker):
        backend.get_lock.return_value = _lock(age=3 * 3600)
        mocker.patch("tfworker.locking.pipe_exec", return_value=(1, b"", b"boom\n"))
        with pytest.raises(
            LockUnavailableError, match="unable to force unlock .*: boom"
        ) as error:
            acquire(
                make_locker(backend, on_stale="force_unlock", on_timeout="fail_run")
            )
        assert error.value.fail_run is True

    def test_force_unlock_error_is_a_lock_failure(self, backend, mocker):
        backend.get_lock.return_value = _lock(age=3 * 3600)
        mocker.patch("tfworker.locking.pipe_exec", side_effect=OSError("no binary"))
        with pytest.raises(LockUnavailableError, match="no binary"):
            acquire(make_locker(backend, on_stale="force_unlock"))

    @pytest.mark.parametrize("after", [None, _lock(age=10)])
    def test_lost_force_unlock_race_keeps_waiting(self, backend, mocker, after):
        stale = _lock(age=3 * 3600)
        other = after.model_copy(update={"id": "lock-2"}) if after else None
        # two stale polls, the re-read after the failed unlock, then free
        backend.get_lock.side_effect = [stale, stale, other, None]
        mocker.patch("tfworker.locking.pipe_exec", return_value=(1, b"", b"gone"))
        started = []
        acquire(make_locker(backend, started=started, on_stale="force_unlock"))
        assert started

    def test_stale_fail_honours_fail_run(self, backend):
        backend.get_lock.return_value = _lock(age=3 * 3600)
        with pytest.raises(LockUnavailableError, match="stale") as error:
            acquire(make_locker(backend, on_timeout="fail_run"))
        assert error.value.fail_run is True


class TestHeartbeat:
    def test_lease_written_and_released(self, backend):
        locker = make_locker(backend, heartbeat=30)
        acquire(locker, APPLY)
        lease = backend.put_lease.call_args.args[0]
        assert lease.definition == "def1"
        assert lease.action == "apply"
        assert lease.host == "pod-9"
        assert lease.who.endswith("@pod-9")
        assert lease.run_id == "run-7"
        assert lease.heartbeat_seconds == 30
        locker.release("def1")
        backend.delete_lease.assert_called_once_with("def1", "pod-9")
        assert locker._heartbeats == {}

    def test_heartbeat_zero_keeps_no_lease(self, backend):
        locker = make_locker(backend, heartbeat=0, wait=30)
        assert acquire(locker) == 1030
        backend.put_lease.assert_not_called()
        locker.release("def1")
        backend.delete_lease.assert_not_called()

    def test_new_action_replaces_previous_lease(self, backend):
        locker = make_locker(backend)
        acquire(locker, PLAN)
        acquire(locker, APPLY)
        backend.delete_lease.assert_called_once_with("def1", "pod-9")
        locker.release("def1")

    def test_release_leaves_another_process_lease(self, backend):
        locker = make_locker(backend)
        acquire(locker)
        backend.get_lease.return_value = _lease(host="pod-9")
        locker.release("def1")
        backend.delete_lease.assert_not_called()

    def test_release_removes_own_lease(self, backend):
        import os

        locker = make_locker(backend)
        acquire(locker)
        own = _lease(host="pod-9").model_copy(
            update={"pid": os.getpid(), "run_id": "run-7"}
        )
        backend.get_lease.return_value = own
        locker.release("def1")
        backend.delete_lease.assert_called_once_with("def1", "pod-9")

    def test_release_deletes_when_lease_unreadable(self, backend):
        locker = make_locker(backend)
        acquire(locker)
        backend.get_lease.side_effect = BackendError("denied")
        locker.release("def1")
        backend.delete_lease.assert_called_once_with("def1", "pod-9")

    def test_no_write_lands_after_stop(self, backend):
        beat = _Heartbeat(backend, _lease(), lambda: NOW)
        beat.start()
        beat.stop()
        backend.put_lease.reset_mock()
        beat._write()
        backend.put_lease.assert_not_called()

    def test_release_without_lease_is_noop(self, backend):
        make_locker(backend).release("def1")
        backend.delete_lease.assert_not_called()

    def test_release_error_is_logged(self, backend):
        backend.delete_lease.side_effect = BackendError("denied")
        locker = make_locker(backend)
        acquire(locker)
        locker.release("def1")
        assert locker._heartbeats == {}

    def test_beat_refreshes_until_stopped(self, backend):
        beat = _Heartbeat(backend, _lease(), lambda: NOW)
        beat._stop = MagicMock()
        beat._stop.wait.side_effect = [False, False, True]
        beat._stop.is_set.return_value = False
        beat._beat()
        assert backend.put_lease.call_count == 2
        assert backend.put_lease.call_args.args[0].heartbeat_at == NOW

    def test_no_write_after_stop(self, backend):
        beat = _Heartbeat(backend, _lease(), lambda: NOW)
        beat._stop = MagicMock()
        beat._stop.wait.return_value = False
        beat._stop.is_set.return_value = True
        beat._beat()
        backend.put_lease.assert_not_called()

    def test_beat_survives_unexpected_errors(self, backend):
        backend.put_lease.side_effect = [RuntimeError("socket"), None]
        beat = _Heartbeat(backend, _lease(), lambda: NOW)
        beat._stop = MagicMock()
        beat._stop.wait.side_effect = [False, False, True]
        beat._stop.is_set.return_value = False
        beat._beat()
        assert backend.put_lease.call_count == 2

    def test_write_error_is_logged(self, backend):
        backend.put_lease.side_effect = BackendError("denied")
        beat = _Heartbeat(backend, _lease(), lambda: NOW)
        beat.start()
        beat.stop()
        assert not beat._thread.is_alive()

    def test_user_fallback(self, mocker):
        mocker.patch("getpass.getuser", side_effect=KeyError("uid"))
        assert StateLocker._user() == "unknown"


class TestLockWaitingEvent:
    def test_announced_once_per_wait(self, backend):
        from types import SimpleNamespace

        import tfworker.report.events as events

        seen = []
        events.subscribe(SimpleNamespace(send=seen.append))
        backend.get_lock.side_effect = [_lock(), _lock(), None]
        acquire(make_locker(backend, wait=600, poll_interval=10), APPLY)
        (event,) = seen
        assert event["type"] == "io.tfworker.lock.waiting"
        assert event["subject"] == "def1"
        assert event["source"] == "urn:tfworker:deployment:dep"
        assert event["tfworkerrunid"] == "run-7"
        assert event["data"]["lock"]["who"] == "runner@pod-1"
        assert event["data"]["wait_seconds"] == 600

    def test_nothing_built_without_sinks(self, backend, mocker):
        publish = mocker.patch("tfworker.report.events.publish")
        backend.get_lock.side_effect = [_lock(), None]
        acquire(make_locker(backend))
        publish.assert_not_called()
