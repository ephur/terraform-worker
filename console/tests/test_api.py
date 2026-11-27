import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from tfworker.report import (
    BackendLocation,
    ChangeSummary,
    DefinitionResult,
    DefinitionStatus,
    ModuleSource,
    PhaseResult,
    PlanManifest,
    Provenance,
    RunRecord,
)
from tfworker.report.models import LOCAL_MODULE_TAG, RunEvent

from tfworker_console import db, queries
from tfworker_console.app import create_app
from tfworker_console.migrate import upgrade
from tfworker_console.queries import Scope
from tfworker_console.reconcile import StoreSummary
from tfworker_console.settings import ConsoleSettings

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def _json(model) -> dict:
    return model.model_dump(mode="json", by_alias=True)


def _state(prefix, definition):
    return BackendLocation(
        backend="s3", state_key=f"{prefix}/{definition}/terraform.tfstate"
    )


def _event(run_id, action, at, status="succeeded", add=0, reason=None):
    return RunEvent(
        run_id=run_id,
        action=action,
        status=status,
        at=at,
        changes=ChangeSummary(add=add),
        reason=reason,
    )


def _status(store, deployment, definition, **events):
    body = DefinitionStatus(
        deployment=deployment,
        definition=definition,
        state=_state(deployment, definition),
        updated_at=NOW,
        updated_by_run="run-1",
        **events,
    )
    return db.Status(
        store=store,
        deployment=deployment,
        definition=definition,
        updated_at=NOW,
        body=_json(body),
    )


def _result(definition, apply_status, ref, tags=(), started=NOW - HOUR):
    return DefinitionResult(
        definition=definition,
        state=_state("p", definition),
        phases={
            "plan": PhaseResult(
                action="plan",
                status="succeeded",
                started_at=started,
                finished_at=started + timedelta(minutes=1),
            ),
            "apply": PhaseResult(
                action="apply",
                status=apply_status,
                started_at=started + timedelta(minutes=1),
                finished_at=started + timedelta(minutes=2),
                reason="exit 1" if apply_status == "failed" else None,
            ),
        },
        module_sources=[
            ModuleSource(
                key="vpc",
                source="git::https://example.com/modules.git",
                kind="git",
                ref=ref,
                sha=ref * 8,
            )
        ],
        tags=list(tags),
    )


def _run(store, deployment, record_id, started, results, status="succeeded"):
    body = RunRecord(
        record_id=record_id,
        run_id=f"run-{record_id}",
        deployment=deployment,
        started_at=started,
        finished_at=started + timedelta(minutes=5),
        status=status,
        actions=["plan", "apply"],
        provenance=Provenance(intent="deploy"),
        definitions=results,
    )
    return db.Run(
        store=store,
        record_id=record_id,
        run_id=body.run_id,
        deployment=deployment,
        status=status,
        started_at=started,
        finished_at=body.finished_at,
        body=_json(body),
    )


def _plan(store, deployment, plan_id, status="pending", storage="backend", at=NOW):
    body = PlanManifest(
        plan_id=plan_id,
        run_id="run-R2",
        deployment=deployment,
        definition="net",
        storage=storage,
        plan_key=f"{deployment}/net/{plan_id}.tfplan",
        sha256="0" * 64,
        size=10,
        changes=ChangeSummary(add=2, destroy=1),
        created_at=at,
        expires_at=at + 24 * HOUR,
    )
    if status != "pending":
        body = body.moved(status, at=at + HOUR, run_id="run-R9", reason="done")
    return db.Plan(
        store=store,
        plan_id=plan_id,
        deployment=deployment,
        definition="net",
        status=body.status,
        created_at=at,
        body=_json(body),
    )


def _lock(store, deployment, definition, who, created, lease=None):
    return db.Lock(
        store=store,
        deployment=deployment,
        definition=definition,
        lock_id=f"lock-{definition}",
        who=who,
        operation="OperationTypeApply",
        created=created,
        lease=lease,
        observed_at=NOW,
    )


def _lease(host, heartbeat_at):
    return {
        "definition": "vpc",
        "action": "apply",
        "who": f"runner@{host}",
        "host": host,
        "pid": 7,
        "started_at": (heartbeat_at - timedelta(minutes=5)).isoformat(),
        "heartbeat_at": heartbeat_at.isoformat(),
        "heartbeat_seconds": 30,
    }


def seed(session):
    for store, name, record_name in (
        ("prod", "alpha", None),
        ("prod", "beta-us", "beta"),
        ("dev", "sandbox", None),
    ):
        session.add(
            db.Deployment(
                store=store,
                name=name,
                prefix=f"tf/{name}",
                record_name=record_name,
                discovered_from="record" if record_name else "template",
                first_seen=NOW - 48 * HOUR,
                last_seen=NOW,
            )
        )
    session.flush()
    for store, deployment, definition in (
        ("prod", "alpha", "net"),
        ("prod", "alpha", "dns"),
        ("prod", "alpha", "vpc"),
        ("prod", "beta-us", "net"),
        ("prod", "beta-us", "dns"),
        ("dev", "sandbox", "net"),
    ):
        session.add(
            db.Definition(
                store=store,
                deployment=deployment,
                name=definition,
                state_key=f"tf/{deployment}/{definition}/terraform.tfstate",
                last_seen=NOW,
            )
        )
    session.add_all(
        [
            # failing: the failure is newer than the last apply
            _status(
                "prod",
                "alpha",
                "net",
                last_apply=_event("run-R0", "apply", NOW - 30 * HOUR),
                last_failure=_event(
                    "run-R1", "apply", NOW - HOUR, "failed", reason="exit 1"
                ),
            ),
            _status(
                "prod",
                "alpha",
                "dns",
                drift=_event("run-R1", "plan", NOW - HOUR, add=1),
            ),
            _status(
                "prod",
                "beta-us",
                "net",
                pending_plan=_event("run-R2", "plan", NOW - 2 * HOUR, add=2),
            ),
            # a failure an apply has since recovered from is ok
            _status(
                "prod",
                "beta-us",
                "dns",
                last_failure=_event("run-R0", "apply", NOW - 30 * HOUR, "failed"),
                last_apply=_event("run-R2", "apply", NOW - 2 * HOUR, add=3),
            ),
            _run(
                "prod",
                "alpha",
                "R1",
                NOW - HOUR,
                [_result("net", "failed", "v1", tags=[LOCAL_MODULE_TAG])],
                status="failed",
            ),
            _run(
                "prod",
                "beta-us",
                "R2",
                NOW - 2 * HOUR,
                [_result("net", "succeeded", "v2", started=NOW - 2 * HOUR)],
            ),
            _run(
                "dev",
                "sandbox",
                "R3",
                NOW - 3 * HOUR,
                [_result("net", "skipped", "v2", started=NOW - 3 * HOUR)],
            ),
            _plan("prod", "beta-us", "P1", at=NOW - 2 * HOUR),
            _plan("prod", "alpha", "P2", storage="preview", at=NOW - HOUR),
            _plan("dev", "sandbox", "P3", status="discarded", at=NOW - 3 * HOUR),
            # live: a fresh lease from the holder
            _lock(
                "prod",
                "alpha",
                "vpc",
                "runner@pod-1",
                NOW - 10 * timedelta(minutes=1),
                _lease("pod-1", NOW - timedelta(seconds=20)),
            ),
            # stale: no lease and older than lock_stale_after
            _lock("prod", "beta-us", "net", "person@laptop", NOW - 3 * HOUR),
            # stale: the holder's lease stopped beating
            _lock(
                "dev",
                "sandbox",
                "net",
                "runner@pod-2",
                NOW - 30 * timedelta(minutes=1),
                _lease("pod-2", NOW - 20 * timedelta(minutes=1)),
            ),
        ]
    )
    session.commit()


@pytest.fixture
def engine(tmp_path):
    # CI also runs these against Postgres
    url = os.environ.get("TFWORKER_CONSOLE_TEST_DATABASE_URL")
    engine = db.engine_for(url or f"sqlite:///{tmp_path}/console.db")
    if url:  # pragma: no cover
        with engine.begin() as connection:
            db.Base.metadata.drop_all(connection)
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    upgrade(engine)
    return engine


@pytest.fixture
def sessions(engine):
    sessions = db.session_factory(engine)
    with sessions() as session:
        seed(session)
    return sessions


@pytest.fixture
def client(engine, sessions):
    settings = ConsoleSettings(
        database_url=engine.url.render_as_string(hide_password=False),
        reconcile_interval=0,
    )
    app = create_app(settings)
    app.state.sessions = sessions
    app.state.clock = lambda: NOW
    return TestClient(app)


def health_of(rows):
    return [(r["deployment"], r["definition"], r["health"]) for r in rows]


class TestFleet:
    def test_exceptions_first_with_counts(self, client):
        body = client.get("/api/v1/fleet").json()
        assert body["counts"] == {
            "failing": 1,
            "drifted": 1,
            "pending": 1,
            "locked": 2,
            "ok": 1,
        }
        assert health_of(body["exceptions"]) == [
            ("alpha", "net", "failing"),
            ("alpha", "dns", "drifted"),
            ("beta-us", "net", "pending"),
            ("sandbox", "net", "locked"),
            ("alpha", "vpc", "locked"),
        ]
        failing = body["exceptions"][0]
        assert failing["last_failure"]["reason"] == "exit 1"
        assert body["exceptions"][2]["locked"] is True

    def test_scoped_to_a_store_and_deployment(self, client):
        body = client.get("/api/v1/fleet?store=prod&deployment=beta-us").json()
        assert body["counts"]["ok"] == 1
        assert health_of(body["exceptions"]) == [("beta-us", "net", "pending")]

    def test_empty_picker_values_mean_all(self, client):
        body = client.get("/api/v1/fleet?store=&deployment=").json()
        assert sum(body["counts"].values()) == 6


def test_deployments(client):
    rows = client.get("/api/v1/deployments").json()
    assert [(r["store"], r["deployment"], r["record_name"]) for r in rows] == [
        ("dev", "sandbox", None),
        ("prod", "alpha", None),
        ("prod", "beta-us", "beta"),
    ]
    alpha = rows[1]
    assert alpha["definitions"] == 3
    assert alpha["counts"] == {
        "failing": 1,
        "drifted": 1,
        "pending": 0,
        "locked": 1,
        "ok": 0,
    }
    scoped = client.get("/api/v1/deployments?store=prod&deployment=alpha").json()
    assert [r["deployment"] for r in scoped] == ["alpha"]


class TestDefinition:
    def test_across_deployments(self, client):
        body = client.get("/api/v1/definitions/net").json()
        assert body["definition"] == "net"
        by = {
            (d["health"]["store"], d["health"]["deployment"]): d
            for d in body["deployments"]
        }
        assert list(by) == [
            ("dev", "sandbox"),
            ("prod", "alpha"),
            ("prod", "beta-us"),
        ]
        assert by[("prod", "beta-us")]["pending_plan"]["plan_id"] == "P1"
        assert by[("prod", "beta-us")]["lock"]["state"] == "stale"
        assert [p["plan_id"] for p in by[("prod", "alpha")]["previews"]] == ["P2"]
        assert by[("prod", "alpha")]["pending_plan"] is None
        assert [r["record_id"] for r in by[("prod", "alpha")]["recent_runs"]] == ["R1"]
        (vpc,) = body["modules"]
        assert vpc["key"] == "vpc" and vpc["skewed"] is True
        assert sorted(v["ref"] for v in vpc["versions"]) == ["v1", "v2", "v2"]

    def test_unknown_is_404(self, client):
        assert client.get("/api/v1/definitions/nope").status_code == 404
        assert client.get("/definitions/nope").status_code == 404

    def test_timeline_newest_first(self, client):
        rows = client.get("/api/v1/definitions/net/timeline").json()
        ats = [r["at"] for r in rows]
        assert ats == sorted(ats, reverse=True)
        kinds = {(r["deployment"], r["kind"], r["action"], r["status"]) for r in rows}
        assert ("alpha", "phase", "apply", "failed") in kinds
        assert ("sandbox", "plan", "plan P3", "discarded") in kinds
        assert len(client.get("/api/v1/definitions/net/timeline?limit=2").json()) == 2

    def test_module_sources_skip_runs_without_them(self, sessions):
        with sessions() as session:
            assert queries._module_sources(session, "prod", "alpha", "dns") == []


class TestLists:
    def test_plans(self, client):
        rows = client.get("/api/v1/plans").json()
        assert [r["plan_id"] for r in rows] == ["P2", "P1", "P3"]
        assert rows[1]["add"] == 2 and rows[1]["destroy_count"] == 1
        pending = client.get("/api/v1/plans?status=pending&definition=net").json()
        assert [r["plan_id"] for r in pending] == ["P2", "P1"]

    def test_locks(self, client):
        rows = client.get("/api/v1/locks").json()
        assert [(r["deployment"], r["state"]) for r in rows] == [
            ("beta-us", "stale"),
            ("sandbox", "stale"),
            ("alpha", "live"),
        ]
        assert rows[2]["age_seconds"] == 600.0
        live = client.get("/api/v1/locks?state=live").json()
        assert [r["definition"] for r in live] == ["vpc"]
        assert client.get("/api/v1/locks?state=other").status_code == 422

    def test_runs(self, client):
        rows = client.get("/api/v1/runs").json()
        assert [r["record_id"] for r in rows] == ["R1", "R2", "R3"]
        assert rows[0]["intent"] == "deploy" and rows[0]["definitions"] == ["net"]
        failed = client.get("/api/v1/runs?status=failed").json()
        assert [r["record_id"] for r in failed] == ["R1"]
        assert client.get("/api/v1/runs?definition=dns").json() == []
        assert len(client.get("/api/v1/runs?limit=1").json()) == 1

    def test_run_record(self, client):
        body = client.get("/api/v1/runs/prod/R1").json()
        assert body["schema"] == "tfworker.run/v1"
        assert client.get("/api/v1/runs/prod/nope").status_code == 404

    def test_local_module_share(self, client):
        weeks = client.get("/api/v1/tracking/local-modules?weeks=2").json()
        assert [w["week"] for w in weeks] == ["2026-W46", "2026-W47", "2026-W48"]
        assert weeks[-1] == {
            "week": "2026-W48",
            "applies": 2,
            "local": 1,
            "share": 0.5,
        }
        assert weeks[0]["share"] == 0.0


def test_naive_run_times_count_as_utc(sessions):
    with sessions() as session:
        row = session.get(db.Run, ("prod", "R1"))
        row.started_at = row.started_at.replace(tzinfo=None)
        weeks = queries.local_module_applies(session, Scope(), NOW, 1)
    assert weeks[-1].applies == 2


def test_locked_is_the_last_exception():
    assert queries._health({}, True) == "locked"
    assert queries._health({}, False) == "ok"


def test_timeline_skips_other_definitions_and_untimed_phases(sessions):
    result = _result("net", "succeeded", "v3")
    result.phases["plan"] = PhaseResult(action="plan", status="skipped")
    with sessions() as session:
        session.add(
            _run(
                "dev",
                "sandbox",
                "R4",
                NOW,
                [_result("dns", "succeeded", "v3"), result],
            )
        )
        session.commit()
        entries = queries.timeline(session, Scope(["dev"]), "net")
    mine = [(e.run_id, e.action) for e in entries if e.run_id == "run-R4"]
    assert mine == [("run-R4", "apply")]


class TestScreens:
    @pytest.mark.parametrize(
        "path, expected",
        [
            ("/", "failing"),
            ("/deployments", "(beta)"),
            ("/definitions/net", "skewed"),
            ("/plans", "P1"),
            ("/plans?status=pending", "P2"),
            ("/locks", "person@laptop"),
            ("/locks?state=", "runner@pod-2"),
            ("/runs", "run-R1"),
            ("/tracking/local-modules", "2026-W48"),
        ],
    )
    def test_every_page_renders(self, client, path, expected):
        response = client.get(path)
        assert response.status_code == 200
        assert expected in response.text
        assert response.text.startswith("<!doctype html>")
        assert "option selected>prod" not in response.text

    def test_htmx_swaps_only_the_content(self, client):
        response = client.get("/?store=prod", headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert "<html" not in response.text
        assert "sandbox" not in response.text

    def test_picker_shows_the_scope(self, client):
        response = client.get("/?store=prod&deployment=alpha")
        assert "<option selected>prod</option>" in response.text
        assert "<option selected>alpha</option>" in response.text

    def test_empty_projection(self, engine):
        settings = ConsoleSettings(reconcile_interval=0)
        app = create_app(settings)
        app.state.sessions = db.session_factory(engine)
        with app.state.sessions() as session:
            for table in reversed(db.Base.metadata.sorted_tables):
                session.execute(table.delete())
            session.commit()
        client = TestClient(app)
        assert "Every definition is ok." in client.get("/").text
        assert "No deployments yet" in client.get("/deployments").text


class TestWithoutADatabase:
    def test_reads_are_unavailable(self):
        client = TestClient(create_app(ConsoleSettings()))
        assert client.get("/api/v1/fleet").status_code == 503
        assert client.get("/").status_code == 503
        assert client.post("/api/v1/reconcile").status_code == 503


class TestReconcileEndpoint:
    def test_all_and_one(self, client, mocker):
        reconciler = client.app.state.reconciler
        mocker.patch.object(
            reconciler,
            "reconcile_all",
            return_value=[StoreSummary(store="prod", fetched=2)],
        )
        reconciler.stores = {"prod": object()}
        mocker.patch.object(
            reconciler, "reconcile", return_value=StoreSummary(store="prod", locks=1)
        )
        everything = client.post("/api/v1/reconcile").json()
        assert everything[0]["store"] == "prod" and everything[0]["fetched"] == 2
        one = client.post("/api/v1/reconcile?store=prod").json()
        assert one == [
            {
                "store": "prod",
                "deployments": 0,
                "fetched": 0,
                "skipped": 0,
                "removed": 0,
                "locks": 1,
                "errors": [],
            }
        ]
        reconciler.reconcile.assert_called_once_with("prod")
        assert client.post("/api/v1/reconcile?store=nope").status_code == 404

    def test_reads_are_not_writes(self, client):
        assert client.get("/api/v1/reconcile").status_code == 405
