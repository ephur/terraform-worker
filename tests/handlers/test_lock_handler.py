from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tfworker.backends.lock import LockInfo, WorkerLease
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.exceptions import BackendError, LockUnavailableError
from tfworker.handlers.lock import LockConfig, LockHandler, OnStale, _Heartbeat

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
PLAN = TerraformAction.PLAN
APPLY = TerraformAction.APPLY


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


@pytest.fixture
def definition():
    return SimpleNamespace(
        name="def1",
        lock_deadline=None,
        lock_fail_run=False,
        get_target_path=lambda working_dir: f"{working_dir}/definitions/def1",
    )


def make_handler(backend, clock=None, started=None, **config):
    handler = LockHandler(LockConfig(**config))
    handler._app_state = SimpleNamespace(
        backend=backend,
        root_options=SimpleNamespace(run_id="run-7"),
        terraform_options=SimpleNamespace(terraform_bin="/usr/bin/terraform"),
        authenticators=[],
    )
    clock = clock or Clock()
    handler._now = clock.now
    handler._monotonic = clock.monotonic
    handler._sleep = clock.sleep
    handler._host = "pod-9"
    if started is not None:
        handler._start_heartbeat = lambda action, d: started.append((action, d.name))
    return handler


def pre(handler, definition, action=PLAN):
    handler.execute(action, TerraformStage.PRE, "dep", definition, "/work")


class TestLockConfig:
    def test_defaults(self):
        config = LockConfig()
        assert config.wait == 600
        assert config.on_stale == OnStale.FAIL
        assert config.actions == [PLAN, APPLY, TerraformAction.DESTROY]

    def test_durations_and_single_action(self):
        config = LockConfig(wait="1h", poll_interval="30s", actions="apply")
        assert config.wait == 3600
        assert config.poll_interval == 30
        assert config.actions == [APPLY]

    @pytest.mark.parametrize(
        "field,value",
        [
            ("poll_interval", 0),
            ("poll_interval", "0s"),
            ("on_timeout", "ignore"),
            ("wait", "soon"),
            ("on_stale", "ignore"),
            ("actions", ["init"]),
            ("unknown", 1),
        ],
    )
    def test_invalid(self, field, value):
        with pytest.raises(ValueError):
            LockConfig(**{field: value})


class TestReadiness:
    def test_ready_with_any_backend(self, backend):
        assert make_handler(backend).is_ready()
        backend.lock_inspection = False
        assert make_handler(backend).is_ready()

    def test_backend_without_inspection_only_sets_deadline(self, backend, definition):
        backend.lock_inspection = False
        started = []
        handler = make_handler(backend, started=started, wait=90)
        pre(handler, definition)
        pre(handler, definition)
        assert definition.lock_deadline == 1090
        backend.get_lock.assert_not_called()
        assert started == []
        assert handler._warned_no_inspection

    def test_not_ready_without_backend(self):
        handler = LockHandler(LockConfig())
        handler._app_state = SimpleNamespace()
        assert not handler.is_ready()

    def test_app_state_from_click(self, mocker):
        ctx = MagicMock()
        mocker.patch("click.get_current_context", return_value=ctx)
        assert LockHandler(LockConfig()).app_state is ctx.obj


class TestAcquire:
    def test_free_lock_proceeds_with_full_timeout(self, backend, definition):
        started = []
        handler = make_handler(backend, started=started, wait=300)
        pre(handler, definition)
        assert started == [(PLAN, "def1")]
        assert definition.lock_deadline == 1300

    def test_waits_for_live_lock(self, backend, definition):
        backend.get_lock.side_effect = [_lock(), _lock(), None]
        clock = Clock()
        started = []
        handler = make_handler(
            backend, clock=clock, started=started, wait=600, poll_interval=10
        )
        pre(handler, definition, APPLY)
        assert len(clock.sleeps) == 2
        assert 8 <= clock.sleeps[0] <= 12
        assert 16 <= clock.sleeps[1] <= 24
        assert definition.lock_deadline == 1600
        assert started == [(APPLY, "def1")]

    def test_backoff_is_capped(self, backend, definition):
        handler = make_handler(backend, poll_interval=10)
        assert 32 <= handler._poll_delay(5) <= 48

    def test_times_out(self, backend, definition):
        backend.get_lock.return_value = _lock()
        clock = Clock()
        handler = make_handler(backend, clock=clock, wait=60, poll_interval=15)
        with pytest.raises(
            LockUnavailableError, match="still locked after waiting 1m00s"
        ):
            pre(handler, definition)
        assert sum(clock.sleeps) == pytest.approx(60)
        assert definition.lock_deadline is None

    def test_on_timeout_fail_run(self, backend, definition):
        backend.get_lock.return_value = _lock()
        handler = make_handler(backend, wait=0, on_timeout="fail_run")
        with pytest.raises(LockUnavailableError) as error:
            pre(handler, definition)
        assert error.value.fail_run is True

    def test_timeout_fails_definition_by_default(self, backend, definition):
        backend.get_lock.return_value = _lock()
        with pytest.raises(LockUnavailableError) as error:
            pre(make_handler(backend, wait=0), definition)
        assert error.value.fail_run is False

    def test_zero_wait_fails_at_once(self, backend, definition):
        backend.get_lock.return_value = _lock()
        clock = Clock()
        handler = make_handler(backend, clock=clock, wait=0)
        with pytest.raises(LockUnavailableError, match="ID lock-1"):
            pre(handler, definition)
        assert clock.sleeps == []

    def test_unreadable_lock_is_left_to_terraform(self, backend, definition):
        backend.get_lock.side_effect = BackendError("denied")
        started = []
        handler = make_handler(backend, started=started)
        pre(handler, definition)
        assert started

    def test_actions_not_configured_are_ignored(self, backend, definition):
        backend.get_lock.return_value = _lock()
        handler = make_handler(backend, actions=["apply"])
        pre(handler, definition, PLAN)
        backend.get_lock.assert_not_called()


class TestStaleLocks:
    def test_old_lock_without_lease_fails(self, backend, definition):
        backend.get_lock.return_value = _lock(age=3 * 3600)
        handler = make_handler(backend)
        with pytest.raises(
            LockUnavailableError, match="stale lock .*force-unlock lock-1"
        ):
            pre(handler, definition)

    def test_old_lock_with_fresh_lease_is_live(self, backend, definition):
        backend.get_lock.side_effect = [_lock(age=3 * 3600), None]
        backend.get_lease.return_value = _lease()
        started = []
        handler = make_handler(backend, started=started)
        pre(handler, definition)
        assert started

    @pytest.mark.parametrize(
        "lease", [_lease(heartbeat_age=600), _lease(host="other-pod")]
    )
    def test_dead_or_foreign_lease_is_stale(self, backend, definition, lease):
        backend.get_lock.return_value = _lock(age=3 * 3600)
        backend.get_lease.return_value = lease
        with pytest.raises(LockUnavailableError, match="stale"):
            pre(make_handler(backend), definition)

    def test_young_lock_with_dead_lease_is_stale(self, backend, definition):
        backend.get_lock.return_value = _lock(age=300)
        backend.get_lease.return_value = _lease(heartbeat_age=200)
        with pytest.raises(LockUnavailableError, match="stale"):
            pre(make_handler(backend), definition)

    def test_lease_left_before_the_lock_is_ignored(self, backend, definition):
        backend.get_lock.side_effect = [_lock(age=60), None]
        backend.get_lease.return_value = _lease(heartbeat_age=3600)
        started = []
        pre(make_handler(backend, started=started), definition)
        assert started

    def test_unreadable_lease_is_treated_as_live(self, backend, definition):
        backend.get_lock.side_effect = [_lock(age=3 * 3600), None]
        backend.get_lease.side_effect = BackendError("denied")
        started = []
        pre(make_handler(backend, started=started), definition)
        assert started

    def test_on_stale_wait(self, backend, definition):
        backend.get_lock.side_effect = [_lock(age=3 * 3600), None]
        clock = Clock()
        started = []
        handler = make_handler(backend, clock=clock, started=started, on_stale="wait")
        pre(handler, definition)
        assert len(clock.sleeps) == 1
        assert started

    def test_on_stale_force_unlock(self, backend, definition, mocker):
        # the lock must look stale on two polls before it is removed
        backend.get_lock.side_effect = [_lock(age=3 * 3600), _lock(age=3 * 3600), None]
        pipe = mocker.patch(
            "tfworker.handlers.lock.pipe_exec", return_value=(0, b"", b"")
        )
        mocker.patch(
            "tfworker.commands.terraform.terraform_env", return_value={"A": "1"}
        )
        started = []
        handler = make_handler(backend, started=started, on_stale="force_unlock")
        pre(handler, definition)
        pipe.assert_called_once_with(
            "/usr/bin/terraform force-unlock -force lock-1",
            cwd="/work/definitions/def1",
            env={"A": "1"},
        )
        assert started

    def test_force_unlock_once_per_lock(self, backend, definition, mocker):
        backend.get_lock.return_value = _lock(age=3 * 3600)
        pipe = mocker.patch(
            "tfworker.handlers.lock.pipe_exec", return_value=(0, b"", b"")
        )
        mocker.patch("tfworker.commands.terraform.terraform_env", return_value={})
        handler = make_handler(backend, on_stale="force_unlock", wait=120)
        with pytest.raises(LockUnavailableError, match="still locked"):
            pre(handler, definition)
        pipe.assert_called_once()

    def test_lock_without_host_uses_age(self, backend, definition):
        backend.get_lock.return_value = _lock(age=3 * 3600, who="")
        with pytest.raises(LockUnavailableError, match="stale"):
            pre(make_handler(backend), definition)
        backend.get_lease.assert_not_called()

    def test_lease_is_read_for_the_lock_host(self, backend, definition):
        backend.get_lock.side_effect = [_lock(age=3 * 3600), None]
        backend.get_lease.return_value = _lease()
        pre(make_handler(backend, started=[]), definition)
        backend.get_lease.assert_called_once_with("def1", "pod-1")

    def test_force_unlock_failure(self, backend, definition, mocker):
        backend.get_lock.return_value = _lock(age=3 * 3600)
        mocker.patch(
            "tfworker.handlers.lock.pipe_exec", return_value=(1, b"", b"boom\n")
        )
        mocker.patch("tfworker.commands.terraform.terraform_env", return_value={})
        handler = make_handler(backend, on_stale="force_unlock")
        with pytest.raises(
            LockUnavailableError, match="unable to force unlock .*: boom"
        ):
            pre(handler, definition)


class TestHeartbeat:
    def test_lease_written_and_released_on_post(self, backend, definition):
        handler = make_handler(backend, heartbeat=30)
        pre(handler, definition, APPLY)
        lease = backend.put_lease.call_args.args[0]
        assert lease.definition == "def1"
        assert lease.action == "apply"
        assert lease.host == "pod-9"
        assert lease.who.endswith("@pod-9")
        assert lease.run_id == "run-7"
        assert lease.heartbeat_seconds == 30
        handler.execute(APPLY, TerraformStage.POST, "dep", definition, "/work")
        backend.delete_lease.assert_called_once_with("def1", "pod-9")
        assert definition.lock_deadline is None
        assert definition.lock_fail_run is False
        assert handler._heartbeats == {}

    def test_heartbeat_zero_keeps_no_lease(self, backend, definition):
        handler = make_handler(backend, heartbeat=0, wait=30)
        pre(handler, definition)
        backend.put_lease.assert_not_called()
        assert definition.lock_deadline == 1030
        handler.execute(PLAN, TerraformStage.POST, "dep", definition, "/work")
        backend.delete_lease.assert_not_called()

    def test_released_on_error_skip_and_teardown(self, backend, definition):
        handler = make_handler(backend)
        pre(handler, definition)
        handler.execute(PLAN, TerraformStage.ERROR, "dep", definition, "/work")
        pre(handler, definition)
        handler.skip(PLAN, "dep", definition, "halted")
        pre(handler, definition)
        handler.teardown("dep", "/work")
        assert backend.delete_lease.call_count == 3

    def test_new_action_replaces_previous_lease(self, backend, definition):
        handler = make_handler(backend)
        pre(handler, definition, PLAN)
        pre(handler, definition, APPLY)
        backend.delete_lease.assert_called_once_with("def1", "pod-9")
        handler.teardown("dep", "/work")

    def test_release_without_lease_is_noop(self, backend, definition):
        handler = make_handler(backend)
        handler.skip(PLAN, "dep", definition, "not needed")
        backend.delete_lease.assert_not_called()

    def test_release_error_is_logged(self, backend, definition):
        backend.delete_lease.side_effect = BackendError("denied")
        handler = make_handler(backend)
        pre(handler, definition)
        handler.teardown("dep", "/work")
        assert handler._heartbeats == {}

    def test_beat_refreshes_until_stopped(self, backend):
        lease = _lease()
        beat = _Heartbeat(backend, lease, lambda: NOW)
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

    def test_fail_run_flag_set_for_terraform(self, backend, definition):
        handler = make_handler(backend, on_timeout="fail_run", started=[])
        pre(handler, definition)
        assert definition.lock_fail_run is True

    def test_write_error_is_logged(self, backend):
        backend.put_lease.side_effect = BackendError("denied")
        beat = _Heartbeat(backend, _lease(), lambda: NOW)
        beat.start()
        beat.stop()
        assert not beat._thread.is_alive()

    def test_user_fallback(self, mocker):
        mocker.patch("getpass.getuser", side_effect=KeyError("uid"))
        assert LockHandler._user() == "unknown"


class TestThroughHandlersCollection:
    @pytest.fixture
    def collection(self, backend):
        from tfworker.handlers.collection import HandlersCollection

        HandlersCollection._instance = None
        yield lambda handler: HandlersCollection({"lock": handler})
        HandlersCollection._instance = None

    def test_timeout_propagates_and_deadline_reaches_command(
        self, backend, definition, collection, tmp_path
    ):
        clock = Clock()
        handler = make_handler(backend, clock=clock, started=[], wait=0)
        handlers = collection(handler)
        backend.get_lock.return_value = _lock()
        with pytest.raises(LockUnavailableError):
            handlers.exec_handlers(
                action=APPLY,
                stage=TerraformStage.PRE,
                deployment="dep",
                definition=definition,
                working_dir="/work",
            )

        backend.get_lock.return_value = None
        handler._monotonic = __import__("time").monotonic
        handler.config.wait = 120
        handlers.exec_handlers(
            action=APPLY,
            stage=TerraformStage.PRE,
            deployment="dep",
            definition=definition,
            working_dir="/work",
        )
        from tests.commands.test_terraform import make_command

        cmd = make_command(tmp_path)
        assert 119 <= cmd._lock_timeout(definition) <= 120


class TestLockWaitingEvent:
    def test_announced_once_per_wait(self, backend, definition):
        import tfworker.report.events as events

        seen = []
        events.subscribe(SimpleNamespace(send=seen.append))
        backend.get_lock.side_effect = [_lock(), _lock(), None]
        handler = make_handler(backend, wait=600, poll_interval=10)
        pre(handler, definition, APPLY)
        (event,) = seen
        assert event["type"] == "io.tfworker.lock.waiting"
        assert event["subject"] == "def1"
        assert event["source"] == "urn:tfworker:deployment:dep"
        assert event["tfworkerrunid"] == "run-7"
        assert event["data"]["lock"]["who"] == "runner@pod-1"
        assert event["data"]["wait_seconds"] == 600
