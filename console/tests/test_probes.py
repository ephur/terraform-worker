import threading
import time
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from tfworker_console import db
from tfworker_console.app import create_app
from tfworker_console.migrate import upgrade
from tfworker_console.probes import TIMED_OUT, Readiness
from tfworker_console.settings import ConsoleSettings


@pytest.fixture
def engine(tmp_path):
    return db.engine_for(f"sqlite:///{tmp_path}/console.db")


def wait_until(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.01)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class TestReadiness:
    def test_ready_when_migrated(self, engine):
        upgrade(engine)
        assert Readiness(engine).check() == (True, {"database": "ok", "schema": "ok"})

    def test_not_migrated(self, engine):
        ready, checks = Readiness(engine).check()
        assert not ready
        assert checks == {"database": "ok", "schema": "not migrated"}

    def test_behind_head(self, engine):
        upgrade(engine, "0003")
        readiness = Readiness(engine)
        ready, checks = readiness.check()
        assert not ready
        assert checks["schema"] == f"at 0003, this console expects {readiness.head}"

    def test_ahead_of_head(self, engine):
        # a newer console migrated the database during a rollout
        upgrade(engine)
        with engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num = '9999'"))
        readiness = Readiness(engine)
        ready, checks = readiness.check()
        assert not ready
        assert checks["schema"] == f"at 9999, this console expects {readiness.head}"

    def test_unreachable_names_only_the_error_type(self, engine):
        readiness = Readiness(engine)
        error = OperationalError("SELECT 1", {}, Exception("db.internal:5432 refused"))
        with mock.patch.object(engine, "connect", side_effect=error):
            ready, checks = readiness.check()
        assert not ready
        assert checks == {"database": "unreachable (OperationalError)"}
        assert "db.internal" not in str(checks)

    def test_timeout(self, engine):
        release = threading.Event()
        readiness = Readiness(engine, timeout=0.05)
        with mock.patch.object(readiness, "_revision", side_effect=release.wait):
            assert readiness.check() == TIMED_OUT
        release.set()

    def test_recovers_once_a_hung_check_returns(self, engine):
        upgrade(engine)
        clock = Clock()
        release = threading.Event()
        readiness = Readiness(engine, timeout=0.05, clock=clock)
        real = readiness._revision
        calls = []

        def revision():
            calls.append(1)
            if len(calls) == 1:
                release.wait(5)
            return real()

        with mock.patch.object(readiness, "_revision", side_effect=revision):
            assert readiness.check() == TIMED_OUT
            clock.now += 2
            # still hung: the same check is waited on, none is started beside it
            assert readiness.check() == TIMED_OUT
            assert len(calls) == 1
            release.set()
            wait_until(lambda: readiness._running is None)
            clock.now += 2
            assert readiness.check() == (True, {"database": "ok", "schema": "ok"})
            assert len(calls) == 2

    def test_a_stuck_check_is_left_behind(self, engine):
        upgrade(engine)
        clock = Clock()
        release = threading.Event()
        readiness = Readiness(engine, timeout=0.05, stuck_seconds=30, clock=clock)
        real = readiness._revision
        calls = []

        def revision():
            calls.append(1)
            if len(calls) == 1:
                release.wait(5)
            return real()

        with mock.patch.object(readiness, "_revision", side_effect=revision):
            assert readiness.check() == TIMED_OUT
            clock.now += 31
            assert readiness.check()[0]
            assert len(calls) == 2
        release.set()

    def test_concurrent_callers_share_one_check(self, engine):
        upgrade(engine)
        release = threading.Event()
        readiness = Readiness(engine, timeout=5)
        real = readiness._revision
        calls = []

        def revision():
            calls.append(1)
            release.wait(5)
            return real()

        answers = []
        with mock.patch.object(readiness, "_revision", side_effect=revision):
            threads = [
                threading.Thread(target=lambda: answers.append(readiness.check()))
                for _ in range(5)
            ]
            for thread in threads:
                thread.start()
            wait_until(lambda: calls)
            release.set()
            for thread in threads:
                thread.join(5)
        assert len(calls) == 1
        assert answers == [(True, {"database": "ok", "schema": "ok"})] * 5

    def test_a_failure_is_cached_too(self, engine):
        clock = Clock()
        readiness = Readiness(engine, clock=clock)
        with mock.patch.object(
            readiness, "_revision", side_effect=OSError("down")
        ) as revision:
            readiness.check()
            clock.now += 0.5
            assert readiness.check() == (False, {"database": "unreachable (OSError)"})
            assert revision.call_count == 1

    def test_percent_in_the_database_url(self, tmp_path):
        engine = db.engine_for(f"sqlite:///{tmp_path}/a%40b.db")
        upgrade(engine)
        assert Readiness(engine).check()[0]

    def test_answer_is_cached_briefly(self, engine):
        upgrade(engine)
        clock = Clock()
        readiness = Readiness(engine, cache_seconds=1.0, clock=clock)
        with mock.patch.object(
            readiness, "_revision", wraps=readiness._revision
        ) as revision:
            readiness.check()
            clock.now += 0.5
            readiness.check()
            assert revision.call_count == 1
            clock.now += 0.6
            readiness.check()
            assert revision.call_count == 2

    def test_no_database(self):
        readiness = Readiness(None)
        assert readiness.head is None
        assert readiness.check() == (True, {"database": "not configured"})


class TestEndpoints:
    def test_readyz_without_database(self):
        response = TestClient(create_app()).get("/readyz")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "checks": {"database": "not configured"},
        }

    def test_readyz_follows_the_schema(self, tmp_path):
        url = f"sqlite:///{tmp_path}/console.db"
        # without auto-migrate the console waits for `db upgrade`
        response = TestClient(create_app(ConsoleSettings(database_url=url))).get(
            "/readyz"
        )
        assert response.status_code == 503
        assert response.json()["checks"]["schema"] == "not migrated"
        settings = ConsoleSettings(database_url=url, auto_migrate=True)
        response = TestClient(create_app(settings)).get("/readyz")
        assert response.status_code == 200
        assert response.json()["checks"] == {"database": "ok", "schema": "ok"}

    def test_readyz_unavailable(self, tmp_path):
        url = f"sqlite:///{tmp_path}/console.db"
        app = create_app(ConsoleSettings(database_url=url, auto_migrate=True))
        error = OSError("db.internal:5432 refused")
        with mock.patch.object(app.state.readiness, "_revision", side_effect=error):
            response = TestClient(app).get("/readyz")
        assert response.status_code == 503
        assert response.json() == {
            "status": "unavailable",
            "checks": {"database": "unreachable (OSError)"},
        }

    def test_readyz_times_out(self, tmp_path):
        url = f"sqlite:///{tmp_path}/console.db"
        app = create_app(ConsoleSettings(database_url=url, auto_migrate=True))
        app.state.readiness.timeout = 0.05
        release = threading.Event()
        with mock.patch.object(
            app.state.readiness, "_revision", side_effect=release.wait
        ):
            client = TestClient(app)
            response = client.get("/readyz")
            assert response.status_code == 503
            assert response.json()["checks"] == {"database": "did not answer in time"}
            # liveness never waits on the database
            assert client.get("/livez").status_code == 200
        release.set()
