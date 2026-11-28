import json
from datetime import datetime, timedelta, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_reconcile import aws  # noqa: F401
from test_reconcile import engine  # noqa: F401
from test_reconcile import sessions  # noqa: F401
from test_reconcile import bucket, put
from tfworker.backends.lock import WorkerLease
from tfworker.report import BackendLocation, PlanManifest
from tfworker.util.plan_manifest import S3ManifestStore

from tfworker_console import db
from tfworker_console.actions import Actions
from tfworker_console.app import create_app
from tfworker_console.auth import Grant, Principal
from tfworker_console.groups import Group, Groups, GroupsFile, Rule
from tfworker_console.reconcile import Reconciler
from tfworker_console.settings import ConsoleSettings
from tfworker_console.stores import StoreConfig

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
BUCKET = "state"
STALE_AFTER = 2 * 3600.0

STORE = StoreConfig(
    name="prod", region="us-east-1", bucket=BUCKET, prefix="tf/{deployment}"
)
GROUPS = Groups(
    GroupsFile(
        groups=[
            Group(name="Production", plan_ttl="1d", force_unlock="admins"),
            Group(name="Sandbox", force_unlock="operators"),
        ],
        rules=[
            Rule(match="sandbox-*", group="Sandbox"),
            Rule(match="*", group="Production"),
        ],
    )
)

ADMIN = Principal("adam", "user", [Grant("admin")])
OPERATOR = Principal("olga", "user", [Grant("operator")])
VIEWER = Principal("vic", "user", [Grant("viewer")])
SANDBOX_OPERATOR = Principal("sam", "user", [Grant("operator", frozenset({"Sandbox"}))])


def state_key(deployment, definition="network"):
    return f"tf/{deployment}/{definition}/terraform.tfstate"


def manifest(deployment="east", plan_id="plan-1", created=NOW - HOUR, **fields):
    values = dict(
        plan_id=plan_id,
        run_id="run-1",
        deployment=deployment,
        definition="network",
        plan_key=f"tf/{deployment}/network/terraform.tfplan",
        sha256="0" * 64,
        size=1,
        created_at=created,
        expires_at=NOW + 7 * 24 * HOUR,
        base_lineage="lineage-1",
        base_serial=3,
    )
    values.update(fields)
    return PlanManifest(**values)


class World:
    """One store in moto, with plans, a state, and locks, projected."""

    def __init__(self, sessions):  # noqa: F811
        self.sessions = sessions
        self.s3 = bucket(BUCKET)

    def plans(self, deployment="east") -> S3ManifestStore:
        return S3ManifestStore(self.s3, BUCKET, f"tf/{deployment}")

    def add_plan(self, plan: PlanManifest) -> str:
        store = self.plans(plan.deployment)
        key = store.record_key(plan.definition, plan.plan_id)
        put(self.s3, BUCKET, plan.plan_key, "plan bytes")
        store.write(key, plan)
        store.point(plan.plan_key, key)
        return key

    def state(self, deployment="east", lineage="lineage-1", serial=3):
        put(
            self.s3,
            BUCKET,
            state_key(deployment),
            json.dumps({"lineage": lineage, "serial": serial}),
        )

    def lock(self, deployment="east", lock_id="lock-1", age=3 * HOUR, who="ci@pod-1"):
        body = {
            "ID": lock_id,
            "Operation": "OperationTypeApply",
            "Who": who,
            "Created": (NOW - age).isoformat(),
        }
        put(self.s3, BUCKET, state_key(deployment) + ".tflock", json.dumps(body))

    def lease(self, deployment="east", beat=NOW, host="pod-1", age=3 * HOUR):
        lease = WorkerLease(
            definition="network",
            action="apply",
            who=f"ci@{host}",
            host=host,
            pid=1,
            started_at=NOW - age,
            heartbeat_at=beat,
            heartbeat_seconds=30,
        )
        put(self.s3, BUCKET, self.lease_key(deployment, host), lease.model_dump_json())

    @staticmethod
    def lease_key(deployment="east", host="pod-1"):
        return f"{state_key(deployment)}.tfworker-lease.{host}"

    def reconcile(self):
        Reconciler([STORE], self.sessions, lambda: NOW, GROUPS).reconcile_all()

    def actions(self) -> Actions:
        return Actions({"prod": STORE}, self.sessions, GROUPS, STALE_AFTER)

    def exists(self, key) -> bool:
        listed = self.s3.list_objects_v2(Bucket=BUCKET, Prefix=key)
        return any(item["Key"] == key for item in listed.get("Contents", []))

    def read(self, key) -> PlanManifest:
        return self.plans().read(key)[0]

    def row(self, plan_id="plan-1") -> db.Plan:
        with self.sessions() as session:
            return session.get(db.Plan, ("prod", plan_id))

    def audit(self):
        with self.sessions() as session:
            return session.scalars(
                select(db.AuditEntry).order_by(db.AuditEntry.id)
            ).all()


@pytest.fixture
def world(aws, sessions):  # noqa: F811
    return World(sessions)


def refused(call, status, match):
    from tfworker_console.actions import ActionError

    with pytest.raises(ActionError, match=match) as raised:
        call()
    assert raised.value.status == status
    return raised.value


class TestPlans:
    def test_discard(self, world):
        key = world.add_plan(manifest())
        world.state()
        world.reconcile()
        result = world.actions().plan(
            OPERATOR, "prod", "plan-1", "discard", NOW, "  superseded   by hand "
        )
        assert (result.outcome, result.status) == ("done", "discarded")
        stored = world.read(key)
        assert stored.status == "discarded"
        change = stored.history[-1]
        assert change.reason == "console: discarded by olga: superseded by hand"
        assert change.at == NOW
        # the plan file goes, the record stays, and the projection follows
        assert not world.exists("tf/east/network/terraform.tfplan")
        assert world.row().status == "discarded"
        (entry,) = world.audit()
        assert (entry.actor, entry.action, entry.outcome) == (
            "olga",
            "plan.discard",
            "done",
        )
        assert (entry.group_name, entry.target, entry.reason) == (
            "Production",
            "plan-1",
            "superseded by hand",
        )

    def test_discard_needs_a_reason(self, world):
        world.add_plan(manifest())
        world.reconcile()
        refused(
            lambda: world.actions().plan(OPERATOR, "prod", "plan-1", "discard", NOW),
            400,
            "give a reason",
        )
        assert world.audit()[0].outcome == "refused"
        assert world.row().status == "pending"

    def test_recheck_finds_a_current_plan(self, world):
        world.add_plan(manifest())
        world.state()
        world.reconcile()
        result = world.actions().plan(OPERATOR, "prod", "plan-1", "recheck", NOW)
        assert (result.outcome, result.status) == ("unchanged", "pending")
        assert world.audit()[0].outcome == "unchanged"

    @pytest.mark.parametrize(
        "lineage, serial, why",
        [
            ("lineage-1", 4, "moved from serial 3 to 4"),
            ("lineage-2", 1, "lineage changed"),
        ],
    )
    def test_recheck_marks_a_moved_state_stale(self, world, lineage, serial, why):
        key = world.add_plan(manifest())
        world.state(lineage=lineage, serial=serial)
        world.reconcile()
        result = world.actions().plan(OPERATOR, "prod", "plan-1", "recheck", NOW)
        assert (result.outcome, result.status) == ("done", "stale")
        assert why in world.read(key).history[-1].reason
        assert world.row().status == "stale"

    def test_recheck_with_a_matching_state_version(self, world):
        world.state(serial=9)
        version = "v1"
        mocked = world.actions()
        world.add_plan(manifest(base_state_version_id=version))
        world.reconcile()
        real = world.s3

        class Versioned:
            def __getattr__(self, name):
                return getattr(real, name)

            def head_object(self, **kwargs):
                return {**real.head_object(**kwargs), "VersionId": version}

        mocked._client_for = lambda store: Versioned()
        result = mocked.plan(OPERATOR, "prod", "plan-1", "recheck", NOW)
        assert result.outcome == "unchanged"

    def test_recheck_without_a_state(self, world):
        world.add_plan(manifest())
        world.add_plan(manifest(plan_id="plan-2", base_lineage=None, base_serial=None))
        world.reconcile()
        actions = world.actions()
        gone = actions.plan(OPERATOR, "prod", "plan-1", "recheck", NOW)
        assert (gone.status, gone.outcome) == ("stale", "done")
        assert "no longer exists" in gone.message
        # a plan made before there was any state still applies to an empty one
        fresh = actions.plan(OPERATOR, "prod", "plan-2", "recheck", NOW)
        assert fresh.outcome == "unchanged"

    def test_recheck_reads_only_the_definitions_own_state(self, world):
        own = BackendLocation(backend="s3", bucket=BUCKET, state_key=state_key("east"))
        world.add_plan(manifest(state=own))
        world.state()
        other = "tf/prod/network/terraform.tfstate"
        world.add_plan(
            manifest(
                plan_id="plan-2",
                state=BackendLocation(backend="s3", state_key=other),
            )
        )
        elsewhere = BackendLocation(
            backend="s3", bucket="other", state_key=state_key("east")
        )
        world.add_plan(manifest(plan_id="plan-3", state=elsewhere))
        world.reconcile()
        actions = world.actions()
        result = actions.plan(OPERATOR, "prod", "plan-1", "recheck", NOW)
        assert result.outcome == "unchanged"
        for plan_id in ("plan-2", "plan-3"):
            refused(
                lambda: actions.plan(OPERATOR, "prod", plan_id, "recheck", NOW),
                409,
                "outside east",
            )

    @pytest.mark.parametrize(
        "plan_key",
        [
            "tf/prod/network/terraform.tfstate",
            "tf/east/network/terraform.tfstate",
            "tf/east/other/terraform.tfplan",
            "tf/east/network/../../prod/network/terraform.tfplan",
        ],
    )
    def test_a_forged_plan_key_is_never_deleted(self, world, plan_key):
        world.add_plan(manifest(plan_key=plan_key))
        world.reconcile()
        result = world.actions().plan(OPERATOR, "prod", "plan-1", "discard", NOW, "x")
        assert result.status == "discarded"
        assert "was left alone" in result.message
        assert world.exists(plan_key)

    def test_recheck_of_an_expired_plan_expires_it(self, world):
        world.add_plan(manifest(expires_at=NOW - HOUR))
        world.reconcile()
        result = world.actions().plan(OPERATOR, "prod", "plan-1", "recheck", NOW)
        assert result.status == "expired"

    @pytest.mark.parametrize("body", ["not json", "[1]"])
    def test_an_unreadable_state_fails(self, world, body):
        world.add_plan(manifest())
        put(world.s3, BUCKET, state_key("east"), body)
        world.reconcile()
        refused(
            lambda: world.actions().plan(OPERATOR, "prod", "plan-1", "recheck", NOW),
            502,
            "could not be changed",
        )
        assert world.audit()[0].outcome == "failed"

    def test_expire_past_the_group_ttl(self, world):
        world.add_plan(manifest(created=NOW - 25 * HOUR))
        world.reconcile()
        result = world.actions().plan(OPERATOR, "prod", "plan-1", "expire", NOW)
        assert result.status == "expired"
        assert "older than the Production plan TTL" in result.message

    def test_expire_past_its_own_expiry_with_a_reason(self, world):
        key = world.add_plan(manifest(expires_at=NOW - HOUR))
        world.reconcile()
        world.actions().plan(OPERATOR, "prod", "plan-1", "expire", NOW, "cleanup")
        assert world.read(key).history[-1].reason == "console: expired by olga: cleanup"

    def test_expire_refuses_a_plan_within_its_ttl(self, world):
        world.add_plan(manifest(deployment="sandbox-ana", created=NOW - 25 * HOUR))
        world.add_plan(manifest(plan_id="plan-2"))
        world.reconcile()
        actions = world.actions()
        # Sandbox has no plan TTL
        refused(
            lambda: actions.plan(OPERATOR, "prod", "plan-1", "expire", NOW),
            409,
            "discard it instead",
        )
        refused(
            lambda: actions.plan(OPERATOR, "prod", "plan-2", "expire", NOW),
            409,
            "discard it instead",
        )

    def test_only_a_pending_plan(self, world):
        key = world.add_plan(manifest())
        world.reconcile()
        # a run takes the plan after the last reconcile
        plans = world.plans()
        plans.write(key, world.read(key).moved("applying", at=NOW))
        refused(
            lambda: world.actions().plan(
                OPERATOR, "prod", "plan-1", "discard", NOW, "x"
            ),
            409,
            "the plan is applying, not pending",
        )
        # the projection catches up with what was found
        assert world.row().status == "applying"

    def test_a_plan_that_changes_while_acting(self, world, mocker):
        world.add_plan(manifest())
        world.reconcile()
        mocker.patch("tfworker_console.actions.move", return_value=None)
        refused(
            lambda: world.actions().plan(
                OPERATOR, "prod", "plan-1", "discard", NOW, "x"
            ),
            409,
            "changed while acting",
        )

    def test_a_missing_manifest(self, world):
        key = world.add_plan(manifest())
        world.reconcile()
        world.s3.delete_object(Bucket=BUCKET, Key=key)
        refused(
            lambda: world.actions().plan(OPERATOR, "prod", "plan-1", "recheck", NOW),
            409,
            "gone or unreadable",
        )

    def test_roles(self, world):
        world.add_plan(manifest())
        world.add_plan(manifest(deployment="sandbox-ana", plan_id="plan-2"))
        world.reconcile()
        actions = world.actions()
        denied = refused(
            lambda: actions.plan(VIEWER, "prod", "plan-1", "recheck", NOW),
            403,
            "operator role on Production",
        )
        assert denied.outcome == "forbidden"
        # a plan in a group the caller can't see is not found, and not audited
        refused(
            lambda: actions.plan(SANDBOX_OPERATOR, "prod", "plan-1", "recheck", NOW),
            404,
            "no plan plan-1",
        )
        assert [e.outcome for e in world.audit()] == ["forbidden"]
        result = actions.plan(SANDBOX_OPERATOR, "prod", "plan-2", "recheck", NOW)
        assert result.status == "stale"

    def test_unknown_plans_and_actions(self, world):
        actions = world.actions()
        refused(lambda: actions.plan(ADMIN, "prod", "nope", "recheck", NOW), 404, "")
        refused(lambda: actions.plan(ADMIN, "prod", "nope", "apply", NOW), 404, "")

    def test_a_store_no_longer_configured(self, world):
        world.add_plan(manifest())
        world.reconcile()
        actions = Actions({}, world.sessions, GROUPS, STALE_AFTER)
        refused(
            lambda: actions.plan(ADMIN, "prod", "plan-1", "recheck", NOW),
            409,
            "not configured",
        )


class TestForceUnlock:
    def unlock(self, world, who=ADMIN, lock_id="lock-1", confirm="network", **kw):
        target = dict(store="prod", deployment="east", definition="network")
        target.update(kw)
        return world.actions().force_unlock(
            who,
            target["store"],
            target["deployment"],
            target["definition"],
            lock_id,
            confirm,
            NOW,
        )

    def test_a_dead_lease(self, world):
        world.lock()
        world.lease(beat=NOW - HOUR)
        world.reconcile()
        result = self.unlock(world)
        assert result.outcome == "done"
        assert "lease last beat" in result.message
        assert not world.exists(state_key("east") + ".tflock")
        assert not world.exists(world.lease_key())
        with world.sessions() as session:
            assert session.scalars(select(db.Lock)).all() == []
        (entry,) = world.audit()
        assert (entry.action, entry.outcome, entry.target) == (
            "lock.force_unlock",
            "done",
            "lock-1",
        )
        assert "held by ci@pod-1 for OperationTypeApply" in entry.detail

    def test_an_old_lock_without_a_lease(self, world):
        world.lock(who="")
        world.reconcile()
        result = self.unlock(world)
        assert "no worker lease" in result.message

    def test_another_hosts_lease_does_not_count(self, world):
        world.lock()
        world.lease(host="pod-2")
        world.reconcile()
        assert self.unlock(world).outcome == "done"
        assert world.exists(world.lease_key(host="pod-2"))

    def test_a_fresh_lease_is_refused(self, world):
        world.lock()
        world.lease(beat=NOW - timedelta(seconds=30))
        world.reconcile()
        refused(lambda: self.unlock(world), 409, "the lock is live")
        assert world.exists(state_key("east") + ".tflock")
        assert world.audit()[0].outcome == "refused"

    def test_a_young_lock_without_a_lease_is_refused(self, world):
        world.lock(age=HOUR)
        world.reconcile()
        refused(lambda: self.unlock(world), 409, "may be live")

    def test_an_unreadable_lease_is_no_lease(self, world):
        world.lock()
        put(world.s3, BUCKET, world.lease_key(), "garbage")
        world.reconcile()
        assert self.unlock(world).outcome == "done"

    def test_the_name_must_be_typed(self, world):
        world.lock()
        world.reconcile()
        refused(lambda: self.unlock(world, confirm="netw"), 400, "type network")
        assert world.exists(state_key("east") + ".tflock")

    def test_a_lock_taken_again_since_the_view(self, world):
        world.lock()
        world.reconcile()
        world.lock(lock_id="lock-2")
        refused(lambda: self.unlock(world), 409, "it is now lock-2")

    def test_a_lock_released_since_the_view(self, world):
        world.lock()
        world.reconcile()
        world.s3.delete_object(Bucket=BUCKET, Key=state_key("east") + ".tflock")
        refused(lambda: self.unlock(world), 409, "already released")
        with world.sessions() as session:
            assert session.scalars(select(db.Lock)).all() == []

    def test_a_lock_replaced_while_removing_it(self, world):
        world.lock()
        world.reconcile()
        actions = world.actions()
        real = world.s3

        class Racing:
            def __getattr__(self, name):
                return getattr(real, name)

            def get_object(self, **kwargs):
                found = real.get_object(**kwargs)
                if kwargs["Key"].endswith(".tflock"):
                    # another process takes a new lock in between
                    world.lock(lock_id="lock-1", age=HOUR * 4)
                return found

        actions._client_for = lambda store: Racing()
        refused(
            lambda: actions.force_unlock(
                ADMIN, "prod", "east", "network", "lock-1", "network", NOW
            ),
            409,
            "changed while removing",
        )
        assert world.exists(state_key("east") + ".tflock")

    def test_policy(self, world):
        world.lock()
        world.lock(deployment="sandbox-ana")
        world.reconcile()
        # Production lets only admins force-unlock
        denied = refused(lambda: self.unlock(world, who=OPERATOR), 403, "admin role")
        assert denied.outcome == "forbidden"
        # Sandbox lets operators
        result = self.unlock(world, who=SANDBOX_OPERATOR, deployment="sandbox-ana")
        assert result.outcome == "done"
        refused(lambda: self.unlock(world, who=SANDBOX_OPERATOR), 404, "no lock")

    def test_store_errors_fail_and_are_audited(self, world):
        world.lock()
        world.reconcile()
        actions = world.actions()
        real = world.s3

        class Broken:
            def __getattr__(self, name):
                return getattr(real, name)

            def get_object(self, **kwargs):
                return real.get_object(Bucket="no-such-bucket", Key=kwargs["Key"])

        actions._client_for = lambda store: Broken()
        refused(
            lambda: actions.force_unlock(
                ADMIN, "prod", "east", "network", "lock-1", "network", NOW
            ),
            502,
            "could not be changed",
        )
        assert world.audit()[0].outcome == "failed"


@pytest.fixture
def client(world, engine, tmp_path):  # noqa: F811
    stores = tmp_path / "stores.yaml"
    stores.write_text(
        "stores:\n  - name: prod\n    region: us-east-1\n    bucket: state\n"
        "    prefix: tf/{deployment}\n"
    )
    groups = tmp_path / "groups.yaml"
    groups.write_text(
        "groups:\n  - name: Production\n    plan_ttl: 1d\n  - name: Sandbox\n"
        "    force_unlock: operators\n"
        "rules:\n  - match: 'sandbox-*'\n    group: Sandbox\n"
        "  - match: '*'\n    group: Production\n"
    )
    app = create_app(
        ConsoleSettings(
            database_url=engine.url.render_as_string(hide_password=False),
            stores_file=str(stores),
            groups_file=str(groups),
            reconcile_interval=0,
        )
    )
    app.state.sessions = world.sessions
    app.state.actions.sessions = world.sessions
    app.state.clock = lambda: NOW
    return TestClient(app)


class TestApi:
    def test_plan_actions(self, world, client):
        world.add_plan(manifest())
        world.state(serial=4)
        world.reconcile()
        response = client.post("/api/v1/plans/prod/plan-1/recheck")
        assert response.status_code == 200
        assert response.json()["status"] == "stale"
        again = client.post(
            "/api/v1/plans/prod/plan-1/discard", json={"reason": "again"}
        )
        assert again.status_code == 409
        assert "not pending" in again.json()["detail"]
        audit = client.get("/api/v1/audit").json()
        assert [e["outcome"] for e in audit] == ["refused", "done"]
        assert audit[0]["reason"] == "again"
        assert client.get("/api/v1/audit?group=Sandbox").json() == []

    def test_force_unlock(self, world, client):
        world.lock()
        world.reconcile()
        url = "/api/v1/locks/prod/east/network/force-unlock"
        assert (
            client.post(url, json={"lock_id": "lock-1", "confirm": "x"}).status_code
            == 400
        )
        response = client.post(url, json={"lock_id": "lock-1", "confirm": "network"})
        assert response.status_code == 200, response.text
        assert client.get("/api/v1/locks").json() == []

    def test_the_demo_cannot_act(self, tmp_path):
        app = create_app(
            ConsoleSettings(
                database_url=f"sqlite:///{tmp_path / 'demo.db'}",
                demo=True,
                reconcile_interval=0,
            )
        )
        client = TestClient(app)
        response = client.post("/api/v1/plans/demo-prod/edge-cdn-10/discard")
        assert response.status_code == 503 and "demo" in response.json()["detail"]
        page = client.post("/plans/demo-prod/edge-cdn-10/discard", data={"reason": "x"})
        assert page.status_code == 200
        assert "not connected to any stores" in page.text


class TestScreens:
    def test_plan_buttons_and_notice(self, world, client):
        world.add_plan(manifest(created=NOW - 25 * HOUR))
        world.reconcile()
        page = client.get("/plans").text
        for action in ("recheck", "expire", "discard"):
            assert f'action="/plans/prod/plan-1/{action}"' in page
        done = client.post(
            "/plans/prod/plan-1/expire?group=Production", follow_redirects=False
        )
        # back to the screen, keeping the scope, so a reload doesn't repeat it
        assert done.status_code == 303
        assert done.headers["location"] == "/plans?group=Production"
        page = client.get(done.headers["location"]).text
        assert 'class="notice ok"' in page and "now expired" in page
        # the notice shows once
        assert "notice" not in client.get("/plans").text.split("<main>")[1]
        refused = client.post("/plans/prod/plan-1/discard", data={"reason": "x"})
        assert refused.url.path == "/plans"
        assert 'class="notice bad"' in refused.text
        audit = client.get("/audit").text
        assert "plan.expire" in audit and "plan.discard" in audit

    def test_force_unlock_form(self, world, client):
        world.lock()
        world.lock(deployment="west", age=HOUR)
        world.reconcile()
        page = client.get("/locks").text
        # only the stale lock offers the button
        assert page.count("Force unlock</button>") == 1
        assert 'action="/locks/prod/east/network/force-unlock"' in page
        done = client.post(
            "/locks/prod/east/network/force-unlock",
            data={"lock_id": "lock-1", "confirm": "network"},
        )
        assert done.url.path == "/locks"
        assert "removed the lock" in done.text

    def test_no_buttons_without_the_role(self, world, client, monkeypatch):
        world.add_plan(manifest())
        world.lock()
        world.reconcile()
        monkeypatch.setattr("tfworker_console.security.DEV_PRINCIPAL", VIEWER)
        assert "Discard</button>" not in client.get("/plans").text
        assert "Force unlock</button>" not in client.get("/locks").text


def test_the_migrations_match_the_models(engine):  # noqa: F811
    with engine.connect() as connection:
        diff = compare_metadata(
            MigrationContext.configure(connection), db.Base.metadata
        )
    assert diff == []


def denied(operation):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "AccessDenied"}}, operation)


@pytest.mark.parametrize(
    "method, match",
    [
        ("head_object", "recheck"),
        ("delete_object", "unlock"),
        ("get_lease", "unlock"),
    ],
)
def test_access_denied_fails_and_is_audited(world, method, match):
    world.add_plan(manifest())
    world.lock()
    world.lease(beat=NOW - HOUR)
    world.reconcile()
    real = world.s3

    class Denying:
        def __getattr__(self, name):
            return getattr(real, name)

        def head_object(self, **kwargs):
            if method == "head_object":
                raise denied("HeadObject")
            return real.head_object(**kwargs)

        def delete_object(self, **kwargs):
            if method == "delete_object":
                raise denied("DeleteObject")
            return real.delete_object(**kwargs)

        def get_object(self, **kwargs):
            if method == "get_lease" and ".tfworker-lease." in kwargs["Key"]:
                raise denied("GetObject")
            return real.get_object(**kwargs)

    actions = world.actions()
    actions._client_for = lambda store: Denying()

    def call():
        if match == "recheck":
            return actions.plan(OPERATOR, "prod", "plan-1", "recheck", NOW)
        return actions.force_unlock(
            ADMIN, "prod", "east", "network", "lock-1", "network", NOW
        )

    refused(call, 502, "AccessDenied")
    assert world.audit()[-1].outcome == "failed"


def test_audit_scope(world, client):
    world.add_plan(manifest())
    world.reconcile()
    client.post("/api/v1/plans/prod/plan-1/recheck")
    assert len(client.get("/api/v1/audit?store=prod&deployment=east").json()) == 1
    assert client.get("/api/v1/audit?deployment=west").json() == []
    assert client.get("/api/v1/audit?store=dev").json() == []


class TestRobustness:
    def test_a_lease_that_cannot_be_removed(self, world):
        world.lock()
        world.lease(beat=NOW - HOUR)
        world.reconcile()
        real = world.s3

        class NoLeaseDelete:
            def __getattr__(self, name):
                return getattr(real, name)

            def delete_object(self, **kwargs):
                if ".tfworker-lease." in kwargs["Key"]:
                    raise denied("DeleteObject")
                return real.delete_object(**kwargs)

        actions = world.actions()
        actions._client_for = lambda store: NoLeaseDelete()
        result = actions.force_unlock(
            ADMIN, "prod", "east", "network", "lock-1", "network", NOW
        )
        # the lock is gone, so it is done, and says what was left
        assert result.outcome == "done"
        assert "dead lease could not be removed (AccessDenied" in result.message
        assert not world.exists(state_key("east") + ".tflock")
        assert world.audit()[-1].outcome == "done"
        with world.sessions() as session:
            assert session.scalars(select(db.Lock)).all() == []

    def test_a_lease_rewritten_meanwhile_is_kept(self, world):
        world.lock()
        world.lease(beat=NOW - HOUR)
        world.reconcile()
        real = world.s3

        class Rewriting:
            def __getattr__(self, name):
                return getattr(real, name)

            def delete_object(self, **kwargs):
                if kwargs["Key"].endswith(".tflock"):
                    world.lease(beat=NOW)
                return real.delete_object(**kwargs)

        actions = world.actions()
        actions._client_for = lambda store: Rewriting()
        result = actions.force_unlock(
            ADMIN, "prod", "east", "network", "lock-1", "network", NOW
        )
        assert "could not be removed" in result.message
        assert world.exists(world.lease_key())

    def test_a_lock_that_is_not_a_lock(self, world):
        world.lock()
        world.reconcile()
        put(world.s3, BUCKET, state_key("east") + ".tflock", "{}")
        refused(lambda: TestForceUnlock().unlock(world), 409, "not a terraform lock")
        assert world.audit()[-1].outcome == "refused"

    def test_an_unexpected_error_is_audited(self, world, mocker):
        world.add_plan(manifest())
        world.reconcile()
        mocker.patch(
            "tfworker_console.actions.move", side_effect=RuntimeError("surprise")
        )
        refused(
            lambda: world.actions().plan(
                OPERATOR, "prod", "plan-1", "discard", NOW, "x"
            ),
            502,
            "RuntimeError",
        )
        assert world.audit()[-1].outcome == "failed"

    def test_the_entry_is_started_before_the_store_is_touched(self, world, mocker):
        world.add_plan(manifest())
        world.reconcile()
        actions = world.actions()
        seen = []
        real = actions._plan

        def spy(*args):
            seen.append([entry.outcome for entry in world.audit()])
            return real(*args)

        mocker.patch.object(actions, "_plan", side_effect=spy)
        actions.plan(OPERATOR, "prod", "plan-1", "discard", NOW, "x")
        assert seen == [["started"]]
        assert [entry.outcome for entry in world.audit()] == ["done"]

    def test_projection_errors_do_not_hide_the_outcome(self, world, mocker, caplog):
        from sqlalchemy.exc import OperationalError

        world.add_plan(manifest())
        world.lock()
        world.reconcile()
        actions = world.actions()
        real = world.sessions
        calls = {"n": 0}

        def flaky():
            # the audit start and end work; the projection update in between fails
            calls["n"] += 1
            if calls["n"] == 3:
                raise OperationalError("UPDATE", {}, Exception("db gone"))
            return real()

        actions.sessions = flaky
        result = actions.plan(OPERATOR, "prod", "plan-1", "discard", NOW, "x")
        assert result.outcome == "done"
        calls["n"] = 0
        result = actions.force_unlock(
            ADMIN, "prod", "east", "network", "lock-1", "network", NOW
        )
        assert result.outcome == "done"
        assert "unable to project plan" in caplog.text
        assert "unable to drop the lock" in caplog.text

    def test_long_values_fit_the_audit_log(self, world, client):
        world.lock()
        world.reconcile()
        url = "/api/v1/locks/prod/east/network/force-unlock"
        long = {"lock_id": "x" * 256, "confirm": "network"}
        assert client.post(url, json=long).status_code == 422
        actions = world.actions()
        refused(
            lambda: actions.force_unlock(
                Principal("a" * 300, "user", [Grant("admin")]),
                "prod",
                "east",
                "network",
                "y" * 300,
                "network",
                NOW,
            ),
            409,
            "lock changed",
        )
        entry = world.audit()[-1]
        assert len(entry.target) == 255 and len(entry.actor) == 255
