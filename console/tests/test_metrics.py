import socket
import time
from datetime import timedelta
from unittest import mock

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy.exc import OperationalError
from test_reconcile import NOW, engine, sessions  # noqa: F401

from tfworker_console import db
from tfworker_console.app import create_app
from tfworker_console.auth import new_token, token_hash, token_principal
from tfworker_console.cli import main
from tfworker_console.groups import Groups, load_groups
from tfworker_console.ingest import DROPPED, EventConsumer
from tfworker_console.metrics import Metrics, MetricsListener, listener_app
from tfworker_console.reconcile import Reconciler, StoreSummary
from tfworker_console.settings import ConsoleSettings


def event(run_id, at, status="succeeded"):
    return {"run_id": run_id, "at": at.isoformat(), "status": status}


def seed(sessions):  # noqa: F811
    """Two deployments in prod, one in dev, with every kind of row."""
    with sessions() as session:
        session.add_all(
            [
                db.Store(
                    name="prod",
                    region="us-east-1",
                    bucket="b",
                    prefix="p/{deployment}",
                    credentials="ambient",
                    reconciled_at=NOW - timedelta(minutes=2),
                ),
                db.Store(
                    name="dev",
                    region="us-east-1",
                    bucket="d",
                    prefix="d/{deployment}",
                    credentials="ambient",
                    reconciled_at=NOW - timedelta(minutes=1),
                    last_error="d/x: AccessDenied",
                ),
                db.Store(
                    name="new",
                    region="us-east-1",
                    bucket="n",
                    prefix="n/{deployment}",
                    credentials="ambient",
                ),
            ]
        )
        for store, name in (("prod", "alpha"), ("prod", "beta"), ("dev", "sandbox")):
            session.add(
                db.Deployment(
                    store=store,
                    name=name,
                    prefix=f"p/{name}",
                    discovered_from="template",
                    first_seen=NOW,
                    last_seen=NOW,
                )
            )
        session.flush()
        for store, deployment, name in (
            ("prod", "alpha", "net"),
            ("prod", "alpha", "dns"),
            ("prod", "beta", "net"),
            ("dev", "sandbox", "net"),
        ):
            session.add(
                db.Definition(
                    store=store, deployment=deployment, name=name, last_seen=NOW
                )
            )
        statuses = {
            # failing: the newest failure is newer than every success
            ("prod", "alpha", "net"): {
                "last_apply": event("r1", NOW - timedelta(hours=3)),
                "last_failure": event("r2", NOW - timedelta(hours=1), "failed"),
            },
            ("prod", "alpha", "dns"): {
                "last_apply": event("r1", NOW - timedelta(hours=2)),
            },
            ("prod", "beta", "net"): {
                "last_apply": event("r3", NOW - timedelta(hours=5), "failed"),
            },
        }
        for (store, deployment, name), body in statuses.items():
            session.add(
                db.Status(
                    store=store,
                    deployment=deployment,
                    definition=name,
                    updated_at=NOW,
                    body=body,
                )
            )
        for plan_id, created, extra in (
            ("P1", NOW - timedelta(minutes=30), {}),
            ("P2", NOW - timedelta(minutes=10), {}),
            ("P3", NOW - timedelta(hours=9), {"storage": "preview"}),
            ("P4", NOW - timedelta(hours=9), {"expires_at": NOW.isoformat()}),
        ):
            session.add(
                db.Plan(
                    store="prod",
                    plan_id=plan_id,
                    deployment="beta",
                    definition="net",
                    status="pending",
                    created_at=created,
                    body={"run_id": "r", **extra},
                )
            )
        for definition, created in (
            ("net", NOW - timedelta(minutes=5)),
            ("dns", NOW - timedelta(hours=5)),
        ):
            session.add(
                db.Lock(
                    store="prod",
                    deployment="alpha",
                    definition=definition,
                    lock_id=f"l-{definition}",
                    who="runner",
                    operation="apply",
                    created=created,
                    lease=None,
                    observed_at=NOW,
                )
            )
        for record_id, definition, phase, local, started in (
            ("R1", "net", "succeeded", False, NOW - timedelta(days=1)),
            ("R2", "dns", "succeeded", True, NOW - timedelta(days=2)),
            ("R3", "net", "skipped", True, NOW - timedelta(days=2)),
            ("R4", "net", "succeeded", True, NOW - timedelta(days=30)),
            ("R5", "dns", None, True, NOW - timedelta(days=1)),
        ):
            session.add(
                db.RunDefinition(
                    store="prod",
                    record_id=record_id,
                    definition=definition,
                    deployment="alpha",
                    started_at=started,
                    phases={"apply": {"status": phase}} if phase else {},
                    module_sources=[],
                    tags=[],
                    local_modules=local,
                )
            )
        session.commit()


def scrape(metrics):
    families = text_string_to_metric_families(metrics.render().decode())
    samples = {}
    for family in families:
        for sample in family.samples:
            labels = tuple(sorted(sample.labels.items()))
            samples[(sample.name, labels)] = sample.value
    return samples


def value(samples, name, **labels):
    return samples.get((f"tfworker_console_{name}", tuple(sorted(labels.items()))))


GROUPS = Groups()


@pytest.fixture
def metrics(sessions):  # noqa: F811
    seed(sessions)
    return Metrics(sessions, GROUPS, stale_after=3600, now=lambda: NOW)


class TestFleet:
    def test_definitions_by_health(self, metrics):
        samples = scrape(metrics)
        place = {"store": "prod", "deployment": "alpha", "group": "Unassigned"}
        assert value(samples, "definitions", health="failing", **place) == 1
        assert value(samples, "definitions", health="locked", **place) == 1
        beta = {"store": "prod", "deployment": "beta", "group": "Unassigned"}
        assert value(samples, "definitions", health="pending", **beta) == 1
        dev = {"store": "dev", "deployment": "sandbox", "group": "Unassigned"}
        assert value(samples, "definitions", health="ok", **dev) == 1
        assert value(samples, "projection_up") == 1

    def test_pending_plans(self, metrics):
        samples = scrape(metrics)
        beta = {"store": "prod", "deployment": "beta", "group": "Unassigned"}
        # the preview and the expired plan are not waiting
        assert value(samples, "pending_plans", **beta) == 2
        assert value(samples, "oldest_pending_plan_age_seconds", **beta) == 1800

    def test_locks(self, metrics):
        samples = scrape(metrics)
        place = {"store": "prod", "deployment": "alpha", "group": "Unassigned"}
        assert value(samples, "locks", state="live", **place) == 1
        assert value(samples, "locks", state="stale", **place) == 1

    def test_last_apply_age(self, metrics):
        samples = scrape(metrics)
        place = {"store": "prod", "deployment": "alpha", "group": "Unassigned"}
        assert value(samples, "last_apply_age_seconds", definition="net", **place) == (
            3 * 3600
        )
        assert value(samples, "last_apply_age_seconds", definition="dns", **place) == (
            2 * 3600
        )
        # a failed last apply says nothing about the last success
        beta = {"store": "prod", "deployment": "beta", "group": "Unassigned"}
        assert (
            value(samples, "last_apply_age_seconds", definition="net", **beta) is None
        )

    def test_without_the_definition_label(self, sessions):  # noqa: F811
        seed(sessions)
        metrics = Metrics(
            sessions, GROUPS, stale_after=3600, definition_label=False, now=lambda: NOW
        )
        samples = scrape(metrics)
        place = {"store": "prod", "deployment": "alpha", "group": "Unassigned"}
        # the oldest in the deployment
        assert value(samples, "last_apply_age_seconds", **place) == 3 * 3600
        assert not any("definition" in dict(labels) for _, labels in samples)

    def test_applies_in_the_window(self, metrics):
        samples = scrape(metrics)
        # R1 and R2 applied this week; R3 skipped, R4 is too old, R5 planned only
        assert value(samples, "applies", store="prod") == 2
        assert value(samples, "local_module_applies", store="prod") == 1

    def test_store_reconciles(self, metrics):
        samples = scrape(metrics)
        assert value(samples, "store_reconcile_ok", store="prod") == 1
        assert value(samples, "store_reconcile_ok", store="dev") == 0
        assert (
            value(samples, "store_last_reconcile_timestamp_seconds", store="prod")
            == (NOW - timedelta(minutes=2)).timestamp()
        )
        # never reconciled: no sample rather than a zero
        assert value(samples, "store_reconcile_ok", store="new") is None

    def test_groups_label(self, sessions, tmp_path):  # noqa: F811
        seed(sessions)
        path = tmp_path / "groups.yaml"
        path.write_text(
            "groups:\n  - name: Production\n"
            "rules:\n  - match: alpha\n    group: Production\n"
        )
        metrics = Metrics(sessions, load_groups(str(path)), 3600, now=lambda: NOW)
        samples = scrape(metrics)
        place = {"store": "prod", "deployment": "alpha", "group": "Production"}
        assert value(samples, "definitions", health="failing", **place) == 1

    def test_unreadable_projection(self, sessions):  # noqa: F811
        broken = mock.Mock(side_effect=OperationalError("SELECT", {}, Exception()))
        metrics = Metrics(broken, GROUPS, 3600)
        samples = scrape(metrics)
        assert value(samples, "projection_up") == 0
        assert value(samples, "definitions", health="ok") is None

    def test_cached_briefly(self, sessions):  # noqa: F811
        seed(sessions)
        clock = mock.Mock(return_value=100.0)
        metrics = Metrics(sessions, GROUPS, 3600, now=lambda: NOW, clock=clock)
        (collector,) = [
            c
            for c in metrics.registry._collector_to_names
            if type(c).__name__ == "FleetCollector"
        ]
        with mock.patch.object(
            collector, "_families", wraps=collector._families
        ) as families:
            metrics.render()
            clock.return_value = 105.0
            metrics.render()
            assert families.call_count == 1
            clock.return_value = 111.0
            metrics.render()
            assert families.call_count == 2

    def test_no_database(self):
        samples = scrape(Metrics(None, GROUPS, 3600))
        assert value(samples, "projection_up") is None


class TestConsoleMetrics:
    def test_reconciles(self, sessions):  # noqa: F811
        metrics = Metrics(None, GROUPS, 3600)
        reconciler = Reconciler([], sessions)
        reconciler.observers.append(metrics.observe_reconcile)
        reconciler.observers.append(mock.Mock(side_effect=RuntimeError("broken")))
        summary = StoreSummary(store="prod", fetched=4, errors=["x", "y"])
        with mock.patch.object(reconciler, "_reconcile", return_value=summary):
            assert reconciler.reconcile("prod") is summary
        samples = scrape(metrics)
        assert value(samples, "reconcile_objects_read_total", store="prod") == 4
        assert value(samples, "reconcile_errors_total", store="prod") == 2
        assert value(samples, "reconcile_duration_seconds", store="prod") >= 0

    def test_events(self):
        metrics = Metrics(None, GROUPS, 3600)
        consumer = EventConsumer(mock.Mock(), "https://sqs.us-east-1.amazonaws.com/1/q")
        consumer.observers.append(metrics.observe_event)
        consumer.observers.append(mock.Mock(side_effect=RuntimeError("broken")))
        assert consumer._handle({"Body": "not json"})
        consumer.ingest.apply.return_value = "applied"
        assert consumer._handle(
            {"Body": '{"specversion": "1.0", "type": "io.tfworker.run.v1"}'}
        )
        assert consumer._handle(
            {"Body": '{"specversion": "1.0", "type": "com.example.anything"}'}
        )
        consumer.ingest.apply.side_effect = RuntimeError("db down")
        assert not consumer._handle(
            {"Body": '{"specversion": "1.0", "type": "io.tfworker.status.v1"}'}
        )
        samples = scrape(metrics)
        assert value(samples, "events_total", type="other", outcome=DROPPED) == 1
        assert value(samples, "events_total", type="other", outcome="applied") == 1
        assert (
            value(samples, "events_total", type="io.tfworker.run.v1", outcome="applied")
            == 1
        )
        assert (
            value(
                samples, "events_total", type="io.tfworker.status.v1", outcome="failed"
            )
            == 1
        )


def add_token(sessions, role, expires_at=None):  # noqa: F811
    token = new_token()
    with sessions() as session:
        session.add(
            db.ApiToken(
                name=f"{role}-{len(token)}-{time.monotonic_ns()}",
                token_hash=token_hash(token),
                role=role,
                groups=None,
                created_by="test",
                created_at=NOW,
                expires_at=expires_at,
            )
        )
        session.commit()
    return token


def app_with(engine, **settings):  # noqa: F811
    url = engine.url.render_as_string(hide_password=False)
    return create_app(ConsoleSettings(database_url=url, **settings))


class TestEndpoint:
    def test_off_by_default(self, engine):  # noqa: F811
        app = app_with(engine)
        assert app.state.metrics is None
        assert TestClient(app).get("/metrics").status_code == 404

    def test_token(self, engine, sessions):  # noqa: F811
        app = app_with(engine, metrics="token")
        client = TestClient(app)
        assert client.get("/metrics").status_code == 401
        viewer = add_token(sessions, "viewer")
        refused = client.get("/metrics", headers={"Authorization": f"Bearer {viewer}"})
        assert refused.status_code == 401
        assert refused.headers["WWW-Authenticate"] == "Bearer"
        token = add_token(sessions, "metrics")
        headers = {"Authorization": f"Bearer {token}"}
        client.get("/livez")
        client.get("/definitions/net")
        client.get("/no/such/page")
        response = client.get("/metrics", headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        body = response.text
        assert 'route="/livez"' in body
        assert 'route="/definitions/{name:path}"' in body
        assert 'route="unmatched"' in body
        assert "/definitions/net" not in body
        assert "tfworker_console_projection_up 1.0" in body

    def test_metrics_token_signs_in_to_nothing_else(
        self, engine, sessions  # noqa: F811
    ):
        app = app_with(engine, metrics="token", auth="local", secret_key="k")
        token = add_token(sessions, "metrics")
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/v1/fleet", headers=headers).status_code == 401
        with sessions() as session:
            assert token_principal(session, token, NOW) is None

    def test_expired_token(self, engine, sessions):  # noqa: F811
        app = app_with(engine, metrics="token")
        token = add_token(sessions, "metrics", expires_at=NOW)
        response = TestClient(app).get(
            "/metrics", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401

    def test_token_mode_needs_a_database(self):
        with pytest.raises(ValueError, match="DATABASE_URL"):
            create_app(ConsoleSettings(metrics="token"))

    def test_errors_are_counted(self, engine, sessions):  # noqa: F811
        app = app_with(engine, metrics="token")

        @app.get("/boom")
        def boom():
            raise RuntimeError("boom")

        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/boom").status_code == 500
        token = add_token(sessions, "metrics")
        body = client.get("/metrics", headers={"Authorization": f"Bearer {token}"}).text
        assert 'route="/boom",status="500"' in body

    def test_port(self, engine):  # noqa: F811
        app = app_with(engine, metrics="port", metrics_port=free_port())
        # the console's own port does not serve it
        assert TestClient(app).get("/metrics").status_code == 404
        body = TestClient(listener_app(app.state.metrics)).get("/metrics").text
        assert "tfworker_console_projection_up 1.0" in body

    def test_wired_to_the_reconciler_and_events(self, engine, tmp_path):  # noqa: F811
        stores = tmp_path / "stores.yaml"
        stores.write_text(
            "stores:\n  - name: prod\n    region: us-east-1\n"
            "    bucket: b\n    prefix: p/{deployment}\n"
        )
        app = app_with(
            engine,
            metrics="token",
            stores_file=str(stores),
            reconcile_interval=0,
            events_queue_url="https://sqs.us-east-1.amazonaws.com/1/q",
        )
        observe = app.state.metrics.observe_reconcile
        assert observe in app.state.reconciler.observers

    def test_port_without_a_database(self):
        app = create_app(ConsoleSettings(metrics="port"))
        body = TestClient(listener_app(app.state.metrics)).get("/metrics").text
        assert "tfworker_console_http_requests" in body
        assert "projection_up" not in body

    def test_listener_serves_while_the_console_does(self, engine):  # noqa: F811
        port = free_port()
        app = app_with(
            engine, metrics="port", metrics_host="127.0.0.1", metrics_port=port
        )
        with TestClient(app):
            response = wait_for(f"http://127.0.0.1:{port}/metrics")
            assert response.status_code == 200
            assert httpx.get(f"http://127.0.0.1:{port}/livez").status_code == 404

    def test_listener_stop_before_start(self):
        MetricsListener(Metrics(None, GROUPS, 3600), "127.0.0.1", 0).stop()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url, seconds=5.0):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return httpx.get(url)
        except httpx.TransportError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


class TestCli:
    def test_metrics_token(self, engine, monkeypatch):  # noqa: F811
        url = engine.url.render_as_string(hide_password=False)
        monkeypatch.setenv("TFWORKER_CONSOLE_DATABASE_URL", url)
        result = CliRunner().invoke(
            main, ["tokens", "create", "scraper", "--role", "metrics"]
        )
        assert result.exit_code == 0, result.output
        listed = CliRunner().invoke(main, ["tokens", "list"])
        assert "scraper\tmetrics" in listed.output
        refused = CliRunner().invoke(
            main,
            ["tokens", "create", "other", "--role", "metrics", "--group", "Prod"],
        )
        assert refused.exit_code != 0
        assert "drop --group" in refused.output
