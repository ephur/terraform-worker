import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from tfworker.report import (
    BackendLocation,
    ChangeSummary,
    DefinitionResult,
    ModuleSource,
    PhaseResult,
    PlanManifest,
    Provenance,
    RunRecord,
)
from tfworker.report.models import LOCAL_MODULE_TAG
from tfworker.report.status import merge_status

from tfworker_console import db, queries
from tfworker_console.app import create_app
from tfworker_console.migrate import upgrade
from tfworker_console.queries import Scope
from tfworker_console.reconcile import Reconciler, StoreSummary
from tfworker_console.settings import ConsoleSettings

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)


def _json(model) -> dict:
    return model.model_dump(mode="json", by_alias=True)


def _state(prefix, definition):
    return BackendLocation(
        backend="s3", state_key=f"{prefix}/{definition}/terraform.tfstate"
    )


def phase(action, status, at, reason=None):
    return PhaseResult(
        action=action,
        status=status,
        started_at=at,
        finished_at=at + timedelta(minutes=1),
        reason=reason,
    )


def result(definition, phases, ref="v1", changes=None, tags=()):
    return DefinitionResult(
        definition=definition,
        state=_state("p", definition),
        phases={p.action: p for p in phases},
        changes=changes,
        module_sources=(
            [
                ModuleSource(
                    key="vpc",
                    source="git::https://example.com/modules.git",
                    kind="git",
                    ref=ref,
                    sha=ref * 8,
                )
            ]
            if ref
            else []
        ),
        tags=list(tags),
    )


def record(deployment, record_id, started, results, intent="deploy"):
    failed = any(p.status == "failed" for r in results for p in r.phases.values())
    return RunRecord(
        record_id=record_id,
        run_id=f"run-{record_id}",
        deployment=deployment,
        started_at=started,
        finished_at=started + timedelta(minutes=5),
        status="failed" if failed else "succeeded",
        actions=["plan", "apply"],
        provenance=Provenance(intent=intent, plan_storage="backend"),
        definitions=results,
    )


def runs_for_seed():
    """(store, record) pairs, oldest first, as a worker would write them."""
    t0, t_fail, t_drift = NOW - 30 * HOUR, NOW - HOUR, NOW - 90 * MINUTE
    t_beta, t_sandbox = NOW - 2 * HOUR, NOW - 3 * HOUR
    add2 = ChangeSummary(add=2, destroy=1)
    return [
        (
            "prod",
            record(
                "alpha",
                "R0",
                t0,
                [
                    result(
                        "net",
                        [
                            phase("plan", "succeeded", t0),
                            phase("apply", "succeeded", t0 + MINUTE),
                        ],
                        changes=add2,
                    ),
                ],
            ),
        ),
        (
            "prod",
            record(
                "beta-us",
                "R6",
                t0,
                [
                    result(
                        "dns",
                        [
                            phase("plan", "succeeded", t0),
                            phase("apply", "failed", t0 + MINUTE, "exit 1"),
                        ],
                        ref=None,
                        changes=add2,
                    ),
                ],
            ),
        ),
        (
            "dev",
            record(
                "sandbox",
                "R3",
                t_sandbox,
                [
                    result(
                        "net",
                        [
                            phase("plan", "succeeded", t_sandbox),
                            phase("apply", "skipped", t_sandbox),
                        ],
                        ref="v2",
                        changes=ChangeSummary(),
                    ),
                ],
            ),
        ),
        (
            "prod",
            record(
                "beta-us",
                "R2",
                t_beta,
                [
                    result(
                        "net",
                        [
                            phase("plan", "succeeded", t_beta),
                            phase("apply", "skipped", t_beta),
                        ],
                        ref="v2",
                        changes=add2,
                    ),
                    result(
                        "dns",
                        [
                            phase("plan", "succeeded", t_beta),
                            phase("apply", "succeeded", t_beta + MINUTE),
                        ],
                        ref=None,
                        changes=add2,
                    ),
                ],
            ),
        ),
        (
            "prod",
            record(
                "alpha",
                "R5",
                t_drift,
                [
                    result(
                        "dns",
                        [phase("plan", "succeeded", t_drift)],
                        ref=None,
                        changes=ChangeSummary(add=1),
                    ),
                ],
                intent="drift",
            ),
        ),
        (
            "prod",
            record(
                "alpha",
                "R1",
                t_fail,
                [
                    result(
                        "net",
                        [
                            phase("plan", "succeeded", t_fail),
                            phase("apply", "failed", t_fail + MINUTE, "exit 1"),
                        ],
                        changes=add2,
                        tags=[LOCAL_MODULE_TAG],
                    ),
                ],
            ),
        ),
    ]


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
    reconciler = Reconciler([], None, now=lambda: NOW)
    statuses = {}
    for store, run in runs_for_seed():
        reconciler._project(
            session, store, run.deployment, "run", run.model_dump_json(by_alias=True)
        )
        for item in run.definitions:
            key = (store, run.deployment, item.definition)
            statuses[key] = merge_status(statuses.get(key), run, item, NOW)
    for (store, deployment, definition), status in statuses.items():
        session.add(
            db.Status(
                store=store,
                deployment=deployment,
                definition=definition,
                updated_at=status.updated_at,
                body=_json(status),
            )
        )
    session.add_all(
        [
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
        assert [r["record_id"] for r in by[("prod", "alpha")]["recent_runs"]] == [
            "R1",
            "R0",
        ]
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

    def test_no_module_sources_recorded(self, client):
        assert client.get("/api/v1/definitions/dns").json()["modules"] == []

    def test_definition_names_with_slashes(self, client, sessions):
        with sessions() as session:
            session.add(
                db.Definition(
                    store="dev",
                    deployment="sandbox",
                    name="team/net",
                    state_key=None,
                    last_seen=NOW,
                )
            )
            session.commit()
        body = client.get("/api/v1/definitions/team/net").json()
        assert body["definition"] == "team/net"
        assert client.get("/api/v1/definitions/team/net/timeline").json() == []
        assert client.get("/definitions/team/net").status_code == 200


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
        assert [r["record_id"] for r in rows] == ["R1", "R5", "R2", "R3", "R6", "R0"]
        assert rows[0]["intent"] == "deploy" and rows[0]["definitions"] == ["net"]
        failed = client.get("/api/v1/runs?status=failed").json()
        assert [r["record_id"] for r in failed] == ["R1", "R6"]
        dns = client.get("/api/v1/runs?definition=dns&store=prod").json()
        assert [r["record_id"] for r in dns] == ["R5", "R2", "R6"]
        assert client.get("/api/v1/runs?definition=nope").json() == []
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
            "applies": 4,
            "local": 1,
            "share": 0.25,
        }
        assert weeks[0]["share"] == 0.0


def test_times_are_stored_and_returned_in_utc(sessions):
    pacific = timezone(timedelta(hours=-8))
    run = record(
        "sandbox",
        "R9",
        (NOW - 30 * MINUTE).astimezone(pacific),
        [result("net", [phase("apply", "succeeded", NOW)])],
    )
    with sessions() as session:
        Reconciler([], None)._project(
            session, "dev", "sandbox", "run", run.model_dump_json(by_alias=True)
        )
        session.commit()
    with sessions() as session:
        row = session.get(db.Run, ("dev", "R9"))
        assert row.started_at == NOW - 30 * MINUTE
        assert row.started_at.utcoffset() == timedelta(0)
        newest = queries.runs(session, Scope(["dev"]), limit=1)
        assert [r.record_id for r in newest] == ["R9"]


class TestHealth:
    def status(self, *runs):
        status = None
        for run in runs:
            status = merge_status(status, run, run.definitions[0], NOW)
        return _json(status)

    def test_a_first_apply_that_fails_is_failing(self):
        run = record("d", "A", NOW, [result("net", [phase("apply", "failed", NOW)])])
        assert queries._health(self.status(run), False, False) == "failing"

    def test_a_clean_plan_after_a_failed_plan_recovers(self):
        failed = record(
            "d", "A", NOW - HOUR, [result("net", [phase("plan", "failed", NOW - HOUR)])]
        )
        clean = record(
            "d",
            "B",
            NOW,
            [result("net", [phase("plan", "succeeded", NOW)], changes=ChangeSummary())],
        )
        assert queries._health(self.status(failed), False, False) == "failing"
        assert queries._health(self.status(failed, clean), False, False) == "ok"

    def test_times_are_compared_as_times(self):
        body = {
            "last_apply": {
                "run_id": "a",
                "at": "2026-11-27T10:00:00Z",
                "status": "succeeded",
            },
            "last_failure": {
                "run_id": "b",
                "at": "2026-11-27T10:00:00.500000Z",
                "status": "failed",
            },
        }
        assert queries._health(body, False, False) == "failing"
        body["last_failure"]["at"] = "2026-11-27T07:00:00-05:00"
        assert queries._health(body, False, False) == "failing"
        body["last_apply"]["at"] = "2026-11-27T07:30:00-05:00"
        assert queries._health(body, False, False) == "ok"

    def test_locked_is_the_last_exception(self):
        assert queries._health({}, True, False) == "locked"
        assert queries._health({}, False, False) == "ok"

    def test_a_discarded_plan_is_no_longer_pending(self, client, sessions):
        with sessions() as session:
            row = session.get(db.Plan, ("prod", "P1"))
            row.status = "discarded"
            session.commit()
        net = client.get("/api/v1/definitions/net").json()
        beta = [d for d in net["deployments"] if d["health"]["deployment"] == "beta-us"]
        assert beta[0]["health"]["health"] == "locked"
        assert beta[0]["health"]["pending_plan"] is None

    def test_an_expired_plan_is_no_longer_pending(self, client):
        client.app.state.clock = lambda: NOW + 30 * HOUR
        plans = {p["plan_id"]: p for p in client.get("/api/v1/plans").json()}
        assert plans["P1"]["expired"] is True and plans["P3"]["expired"] is False
        fleet = client.get("/api/v1/fleet").json()
        assert fleet["counts"]["pending"] == 0
        net = client.get("/api/v1/definitions/net").json()
        assert all(d["pending_plan"] is None for d in net["deployments"])


def test_a_live_lock_stays_live_between_reconciles(client):
    client.app.state.clock = lambda: NOW + 5 * MINUTE
    (lock,) = client.get("/api/v1/locks?state=live").json()
    assert lock["definition"] == "vpc"
    assert lock["observed_seconds_ago"] == 300.0
    assert lock["age_seconds"] == 900.0


def test_timeline_skips_other_definitions_and_untimed_phases(sessions):
    untimed = PhaseResult(action="plan", status="skipped")
    run = record(
        "sandbox",
        "R4",
        NOW,
        [
            result("dns", [phase("apply", "succeeded", NOW)]),
            result("net", [untimed, phase("apply", "succeeded", NOW)]),
        ],
    )
    with sessions() as session:
        Reconciler([], None)._project(
            session, "dev", "sandbox", "run", run.model_dump_json(by_alias=True)
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

    def test_the_scope_follows_navigation(self, client):
        page = client.get("/runs?store=prod&deployment=alpha").text
        assert 'hx-get="/runs"' in page
        assert 'href="/plans?store=prod&amp;deployment=alpha"' in page
        assert "/definitions/net?store=prod" not in page  # runs link records
        fleet = client.get("/?store=prod").text
        assert 'href="/definitions/net?store=prod"' in fleet
        assert 'href="/locks"' in client.get("/").text

    def test_partials_vary_and_history_restores_get_the_page(self, client):
        partial = client.get("/", headers={"HX-Request": "true"})
        assert partial.headers["Vary"] == "HX-Request"
        restore = client.get(
            "/",
            headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
        )
        assert restore.text.startswith("<!doctype html>")

    def test_htmx_is_pinned_with_integrity(self, client):
        assert 'integrity="sha384-' in client.get("/").text

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


def test_utc_datetime_type():
    from sqlalchemy.dialects import postgresql, sqlite

    column = db.UTCDateTime()
    pg, lite = postgresql.dialect(), sqlite.dialect()
    eastern = datetime(2026, 11, 27, 7, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert column.process_bind_param(None, pg) is None
    assert column.process_result_value(None, pg) is None
    assert column.process_bind_param(eastern, pg) == NOW
    assert column.process_bind_param(eastern, lite) == NOW.replace(tzinfo=None)
    assert column.process_bind_param(NOW.replace(tzinfo=None), pg) == NOW
    assert column.process_result_value(eastern, pg).tzinfo == timezone.utc
    assert column.process_result_value(NOW.replace(tzinfo=None), lite) == NOW
