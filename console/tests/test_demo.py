from datetime import datetime, timezone
from unittest import mock

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from test_api import engine  # noqa: F401
from test_api import sessions  # noqa: F401

from tfworker_console import db, demo
from tfworker_console.app import create_app
from tfworker_console.cli import main
from tfworker_console.settings import ConsoleSettings

SCREENS = [
    "/",
    "/deployments",
    "/plans",
    "/locks",
    "/runs",
    "/groups",
    "/tracking/local-modules",
    "/definitions/network",
    "/definitions/database",
]


def standalone(tmp_path, **overrides) -> ConsoleSettings:
    """The settings the image runs with."""
    values = dict(
        database_url=f"sqlite:///{tmp_path / 'console.db'}",
        auto_migrate=True,
        demo=True,
        reconcile_interval=0,
    )
    values.update(overrides)
    return ConsoleSettings(**values)


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(standalone(tmp_path)))


class TestDemo:
    def test_every_screen_renders(self, client):
        for path in SCREENS:
            response = client.get(path)
            assert response.status_code == 200, path
        assert client.get("/api/v1/runs/demo-prod/payments-009").status_code == 200

    def test_it_shows_every_health_state(self, client):
        fleet = client.get("/api/v1/fleet").json()
        assert fleet["counts"] == {
            "failing": 1,
            "drifted": 1,
            "pending": 2,
            "locked": 2,
            "ok": 8,
        }
        locks = {
            lock["deployment"]: lock["state"]
            for lock in client.get("/api/v1/locks").json()
        }
        assert locks == {"search": "live", "sandbox-raj": "stale"}
        over = [
            p["plan_id"] for p in client.get("/api/v1/plans").json() if p["over_ttl"]
        ]
        assert over == ["edge-cdn-10"]
        weeks = client.get("/api/v1/tracking/local-modules").json()
        assert any(week["local"] for week in weeks)

    def test_it_uses_the_demo_groups_without_a_groups_file(self, client):
        groups = [group["name"] for group in client.get("/api/v1/groups").json()]
        assert groups == ["Production", "Sandbox", "Unassigned"]

    def test_a_groups_file_still_wins(self, tmp_path):
        path = tmp_path / "groups.yaml"
        path.write_text(
            "groups:\n  - name: Everything\nrules:\n  - match: '*'\n    group: Everything\n"
        )
        app = create_app(standalone(tmp_path, groups_file=str(path)))
        groups = TestClient(app).get("/api/v1/groups").json()
        assert [group["name"] for group in groups] == ["Everything", "Unassigned"]
        assert len(groups[0]["deployments"]) == 5

    def test_a_restart_keeps_the_demo(self, tmp_path):
        first = TestClient(create_app(standalone(tmp_path)))
        runs = len(first.get("/api/v1/runs?limit=500").json())
        again = TestClient(create_app(standalone(tmp_path)))
        assert len(again.get("/api/v1/runs?limit=500").json()) == runs

    def test_reconciling_is_refused(self, client):
        response = client.post("/api/v1/reconcile")
        assert response.status_code == 503 and "demo" in response.json()["detail"]

    def test_it_refuses_a_database_with_real_data(self, sessions):  # noqa: F811
        # the fixture seeds real-looking stores
        with sessions() as session:
            with pytest.raises(demo.DemoRefused, match="empty database"):
                demo.seed(session, datetime.now(timezone.utc))

    def test_a_store_alone_is_data(self, engine):  # noqa: F811
        # a real store whose first reconcile found nothing yet
        with db.session_factory(engine)() as session:
            session.add(
                db.Store(
                    name="demo-prod",
                    region="us-east-1",
                    bucket="b",
                    prefix="p/{deployment}",
                    credentials="ambient",
                )
            )
            session.commit()
            with pytest.raises(demo.DemoRefused):
                demo.seed(session, datetime.now(timezone.utc))

    def test_a_racing_replica(self, tmp_path, mocker):
        TestClient(create_app(standalone(tmp_path)))
        # the other replica's seed won: this one finds the demo already there
        mocker.patch.object(demo, "is_seeded", side_effect=[False, True])
        TestClient(create_app(standalone(tmp_path)))

    def test_dropping_the_flag_removes_the_demo(self, tmp_path, caplog):
        TestClient(create_app(standalone(tmp_path)))
        plain = TestClient(create_app(standalone(tmp_path, demo=False)))
        assert not any(plain.get("/api/v1/fleet").json()["counts"].values())
        assert "removed the demo data" in caplog.text

    def test_it_seeds_once(self, engine):  # noqa: F811
        now = datetime.now(timezone.utc)
        with db.session_factory(engine)() as session:
            assert demo.seed(session, now) is True
            assert demo.seed(session, now) is False
            assert session.query(db.Store).count() == 2

    @pytest.mark.parametrize(
        "overrides, message",
        [
            ({"database_url": None}, "needs TFWORKER_CONSOLE_DATABASE_URL"),
            ({"stores_file": "stores.yaml"}, "leave out TFWORKER_CONSOLE_STORES_FILE"),
        ],
    )
    def test_settings_it_refuses(self, tmp_path, overrides, message):
        with pytest.raises(ValueError, match=message):
            create_app(standalone(tmp_path, **overrides))


def test_auto_migrate_creates_the_schema(tmp_path):
    settings = standalone(tmp_path, demo=False)
    create_app(settings)
    tables = inspect(db.engine_for(settings.database_url)).get_table_names()
    assert {"deployments", "users", "alembic_version"} <= set(tables)
    # and is harmless once the schema is current
    create_app(settings)


def test_without_auto_migrate_the_schema_is_left_alone(tmp_path):
    settings = standalone(tmp_path, demo=False, auto_migrate=False)
    create_app(settings)
    assert inspect(db.engine_for(settings.database_url)).get_table_names() == []


def test_the_demo_migrates_by_itself(tmp_path):
    settings = standalone(tmp_path, auto_migrate=False)
    fleet = TestClient(create_app(settings)).get("/api/v1/fleet").json()
    assert fleet["counts"]["failing"] == 1


def test_serve_demo(monkeypatch):
    # serve writes its options to the environment; set them so they are restored
    monkeypatch.setenv("TFWORKER_CONSOLE_DEMO", "false")
    with mock.patch("tfworker_console.cli.uvicorn.run"):
        result = CliRunner().invoke(main, ["serve", "--demo"])
    assert result.exit_code == 0, result.output
    assert ConsoleSettings().demo is True
