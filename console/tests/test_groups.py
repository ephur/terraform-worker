import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_api import engine  # noqa: F401
from test_api import sessions  # noqa: F401
from test_api import HOUR, NOW, _plan, seed  # noqa: F401  (fixtures reuse the seed)

from tfworker_console import queries
from tfworker_console.app import create_app
from tfworker_console.groups import UNASSIGNED, Groups, GroupsFile, load_groups
from tfworker_console.queries import Scope
from tfworker_console.settings import ConsoleSettings

CONFIG = {
    "groups": [
        {"name": "Production", "plan_ttl": "1h", "force_unlock": "admins"},
        {"name": "Sandbox", "force_unlock": "operators"},
    ],
    "rules": [
        {"match": "beta-*", "group": "Production"},
        {"store": "dev", "match": "*", "group": "Sandbox"},
        {"match": "*", "group": "Sandbox"},
    ],
    "deployments": {"prod/alpha": "Production"},
}


def groups(config=CONFIG) -> Groups:
    return Groups(GroupsFile.model_validate(config))


class TestAssignment:
    def test_first_matching_rule_wins(self):
        resolver = groups(
            {
                "groups": [{"name": "A"}, {"name": "B"}],
                "rules": [
                    {"match": "prod-*", "group": "A"},
                    {"match": "prod-eu-*", "group": "B"},
                ],
            }
        )
        assert resolver.of("s", "prod-eu-1") == "A"

    def test_store_scoped_rule(self):
        resolver = groups(
            {
                "groups": [{"name": "A"}, {"name": "B"}],
                "rules": [
                    {"store": "dev", "match": "app", "group": "B"},
                    {"match": "app", "group": "A"},
                ],
            }
        )
        assert resolver.of("dev", "app") == "B"
        assert resolver.of("prod", "app") == "A"

    def test_manual_assignment_beats_rules(self):
        resolver = groups()
        assert resolver.of("prod", "alpha") == "Production"
        assert resolver.of("dev", "alpha") == "Sandbox"

    def test_unmatched_is_unassigned(self):
        resolver = groups({"groups": [{"name": "A"}], "rules": []})
        assert resolver.of("s", "anything") == UNASSIGNED
        assert resolver.policy("s", "anything").force_unlock == "admins"
        assert resolver.policy("s", "anything").plan_ttl is None

    def test_matching_is_case_sensitive(self):
        resolver = groups(
            {"groups": [{"name": "A"}], "rules": [{"match": "prod-*", "group": "A"}]}
        )
        assert resolver.of("s", "PROD-1") == UNASSIGNED

    def test_sort_order(self):
        resolver = groups()
        assert resolver.names == ["Production", "Sandbox", UNASSIGNED]
        assert resolver.rank("Sandbox") == 1
        assert resolver.rank("gone") == 3

    def test_no_groups(self):
        assert Groups().of("s", "d") == UNASSIGNED
        assert Groups().names == [UNASSIGNED]


class TestConfig:
    def test_durations(self):
        assert groups().group("Production").plan_ttl == 3600
        assert groups({"groups": [{"name": "A", "plan_ttl": None}]}).group(
            "A"
        ).plan_ttl is None
        assert (
            groups({"groups": [{"name": "A", "plan_ttl": 90}]}).group("A").plan_ttl
            == 90
        )

    @pytest.mark.parametrize(
        "config, message",
        [
            ({"groups": [{"name": UNASSIGNED}]}, "built in"),
            ({"groups": [{"name": "A"}, {"name": "A"}]}, "duplicate groups"),
            ({"groups": [{"name": "A"}, {"name": "a"}]}, "names ignore case"),
            ({"groups": [{"name": "unassigned"}]}, "built in"),
            ({"groups": [{"name": " A "}]}, "spaces"),
            ({"groups": [{"name": "A", "plan_ttl": 0}]}, "longer than zero"),
            (
                {"groups": [{"name": "A"}], "rules": [{"match": " a*", "group": "A"}]},
                "spaces",
            ),
            ({"groups": [{"name": "A"}], "deployments": {"s/*": "A"}}, "not a pattern"),
            ({"rules": [{"match": "*", "group": "nope"}]}, "unknown group nope"),
            ({"deployments": {"no-slash": "A"}}, "store/deployment"),
            ({"deployments": {"s/": "A"}}, "store/deployment"),
            ({"deployments": {"s/d": "nope"}}, "unknown group nope"),
            ({"groups": [{"name": "A", "plan_ttl": "soon"}]}, "invalid duration"),
            ({"groups": [{"name": "A", "force_unlock": "anyone"}]}, "force_unlock"),
            ({"groups": [{"name": "A", "extra": 1}]}, "extra"),
        ],
    )
    def test_rejected(self, config, message):
        with pytest.raises(ValidationError, match=message):
            GroupsFile.model_validate(config)

    def test_load(self, tmp_path):
        assert load_groups(None).names == [UNASSIGNED]
        path = tmp_path / "groups.yaml"
        path.write_text("")
        assert load_groups(str(path)).names == [UNASSIGNED]
        path.write_text(
            "groups:\n  - name: Production\nrules:\n  - match: '*'\n"
            "    group: Production\n"
        )
        assert load_groups(str(path)).of("s", "d") == "Production"


@pytest.fixture
def client(engine, sessions, tmp_path):  # noqa: F811
    import yaml

    path = tmp_path / "groups.yaml"
    path.write_text(yaml.safe_dump(CONFIG))
    settings = ConsoleSettings(
        database_url=engine.url.render_as_string(hide_password=False),
        groups_file=str(path),
        reconcile_interval=0,
    )
    app = create_app(settings)  # assigns the seeded deployments to groups
    app.state.sessions = sessions
    app.state.clock = lambda: NOW
    return TestClient(app)


def _deployment(store, name):
    from tfworker_console import db

    return db.Deployment(
        store=store,
        name=name,
        prefix=f"tf/{name}",
        discovered_from="template",
        first_seen=NOW,
        last_seen=NOW,
    )


class TestViews:
    def test_every_view_names_the_group(self, client):
        rows = client.get("/api/v1/deployments").json()
        assert [(r["deployment"], r["group"]) for r in rows] == [
            ("alpha", "Production"),
            ("beta-us", "Production"),
            ("sandbox", "Sandbox"),
        ]
        assert {r["group"] for r in client.get("/api/v1/runs").json()} == {
            "Production",
            "Sandbox",
        }

    def test_fleet_sorts_by_group_within_health(self, client):
        exceptions = client.get("/api/v1/fleet").json()["exceptions"]
        # both locked: Production's comes before Sandbox's
        locked = [
            (e["group"], e["deployment"]) for e in exceptions if e["health"] == "locked"
        ]
        assert locked == [("Production", "alpha"), ("Sandbox", "sandbox")]

    def test_scope_by_group(self, client):
        body = client.get("/api/v1/fleet?group=Sandbox").json()
        assert {e["deployment"] for e in body["exceptions"]} == {"sandbox"}
        assert sum(body["counts"].values()) == 1
        plans = client.get("/api/v1/plans?group=Production").json()
        assert sorted(p["plan_id"] for p in plans) == ["P1", "P2"]
        both = client.get("/api/v1/deployments?group=Production&group=Sandbox")
        assert len(both.json()) == 3
        assert client.get(f"/api/v1/runs?group={UNASSIGNED}").json() == []
        page = client.get("/?group=nope").text
        assert "<option selected>nope</option>" in page
        assert client.get("/api/v1/locks?group=nope").json() == []
        assert client.get("/api/v1/fleet?group=").json()["counts"]["failing"] == 1

    def test_group_and_store_scopes_combine(self, client):
        rows = client.get("/api/v1/deployments?group=Production&store=dev").json()
        assert rows == []

    def test_plan_ttl_is_applied_per_group(self, client, sessions):  # noqa: F811
        # a pending saved plan in Sandbox, which has no TTL, as old as P1
        with sessions() as session:
            session.add(_plan("dev", "sandbox", "P4", at=NOW - 2 * HOUR))
            session.commit()
        plans = {p["plan_id"]: p for p in client.get("/api/v1/plans").json()}
        # P1 is pending in Production, created 2h ago against a 1h TTL
        assert plans["P1"]["over_ttl"] is True
        assert plans["P1"]["ttl_expires_at"].startswith("2026-11-27T11:00")
        # the manifest's own expiry is untouched, so the worker would apply it
        assert plans["P1"]["expired"] is False
        assert plans["P1"]["expires_at"].startswith("2026-11-28T10:00")
        # P4 is just as old, but Sandbox has no TTL
        assert plans["P4"]["over_ttl"] is False
        assert plans["P4"]["ttl_expires_at"] is None
        # previews and settled plans are never over the TTL
        assert (
            plans["P2"]["over_ttl"] is False and plans["P2"]["ttl_expires_at"] is None
        )
        assert plans["P3"]["over_ttl"] is False
        # an over-TTL plan still counts as pending
        fleet = client.get("/api/v1/fleet").json()
        pending = {
            (e["deployment"], e["definition"])
            for e in fleet["exceptions"]
            if e["health"] == "pending"
        }
        assert pending == {("beta-us", "net"), ("sandbox", "net")}
        assert "over TTL" in client.get("/plans").text

    def test_same_name_in_two_stores(self, client, sessions):  # noqa: F811
        with sessions() as session:
            # prod/alpha is Production by hand; dev/alpha falls to the dev rule
            session.add(_deployment("dev", "alpha"))
            session.commit()
            client.app.state.groups.assign(session)
        prod = client.get("/api/v1/deployments?group=Production").json()
        assert [(d["store"], d["deployment"]) for d in prod] == [
            ("prod", "alpha"),
            ("prod", "beta-us"),
        ]
        sandbox = client.get("/api/v1/deployments?group=Sandbox").json()
        assert [(d["store"], d["deployment"]) for d in sandbox] == [
            ("dev", "alpha"),
            ("dev", "sandbox"),
        ]

    def test_group_and_deployment_scopes_combine(self, client):
        rows = client.get("/api/v1/plans?group=Production&deployment=alpha").json()
        assert [p["plan_id"] for p in rows] == ["P2"]
        assert client.get("/api/v1/plans?group=Sandbox&deployment=alpha").json() == []

    def test_unassigned_in_the_views(self, engine, sessions, tmp_path):  # noqa: F811
        import yaml

        path = tmp_path / "groups.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "groups": [{"name": "Production"}],
                    "deployments": {"prod/alpha": "Production"},
                }
            )
        )
        app = create_app(
            ConsoleSettings(
                database_url=engine.url.render_as_string(hide_password=False),
                groups_file=str(path),
                reconcile_interval=0,
            )
        )
        app.state.sessions = sessions
        app.state.clock = lambda: NOW
        client = TestClient(app)
        rows = client.get(f"/api/v1/deployments?group={UNASSIGNED}").json()
        assert [(r["deployment"], r["group"]) for r in rows] == [
            ("sandbox", UNASSIGNED),
            ("beta-us", UNASSIGNED),
        ]
        assert {
            r["group"] for r in client.get(f"/api/v1/runs?group={UNASSIGNED}").json()
        } == {UNASSIGNED}

    def test_force_unlock_policy_is_per_group(self, client):
        locks = {
            lock["deployment"]: lock["force_unlock"]
            for lock in client.get("/api/v1/locks").json()
        }
        assert locks == {
            "alpha": "admins",
            "beta-us": "admins",
            "sandbox": "operators",
        }

    def test_groups_endpoint(self, client):
        rows = client.get("/api/v1/groups").json()
        assert [(g["name"], g["deployments"]) for g in rows] == [
            ("Production", ["prod/alpha", "prod/beta-us"]),
            ("Sandbox", ["dev/sandbox"]),
            (UNASSIGNED, []),
        ]
        assert rows[0]["plan_ttl"] == 3600 and rows[0]["counts"]["failing"] == 1
        assert rows[1]["force_unlock"] == "operators"

    def test_groups_view_ignores_a_group_scope(self, client):
        rows = client.get("/api/v1/groups?group=Sandbox").json()
        assert rows[0]["deployments"] == ["prod/alpha", "prod/beta-us"]
        page = client.get("/groups?store=prod").text
        assert 'href="/?group=Production&amp;store=prod"' in page

    def test_screens(self, client):
        page = client.get("/groups").text
        assert "1h00m" in page and "prod/beta-us" in page
        fleet = client.get("/?group=Sandbox").text
        assert "<option selected>Sandbox</option>" in fleet
        assert "over TTL" in client.get("/plans").text
        assert "unlock: operators" in client.get("/locks").text


def test_without_groups_everything_is_unassigned(sessions):  # noqa: F811
    with sessions() as session:
        rows = queries.deployments(session, Scope(), NOW)
        assert {row.group for row in rows} == {UNASSIGNED}
        plans = queries.plans(session, Scope(), NOW)
        assert not any(plan.expired for plan in plans)


def test_unknown_stores_are_reported(tmp_path, caplog):
    import yaml

    path = tmp_path / "groups.yaml"
    path.write_text(yaml.safe_dump(CONFIG))
    resolver = load_groups(str(path))
    assert resolver.unknown_stores(["dev"]) == ["prod"]
    create_app(
        ConsoleSettings(
            database_url=f"sqlite:///{tmp_path}/empty.db",
            groups_file=str(path),
            reconcile_interval=0,
        )
    )
    assert "names stores that are not configured: dev, prod" in caplog.text
    # the empty database has no tables yet, which is only a warning
    assert "run db upgrade?" in caplog.text


def test_reconcile_stores_each_deployments_group(tmp_path):
    from tfworker_console import db
    from tfworker_console.migrate import upgrade
    from tfworker_console.reconcile import Reconciler

    database = db.engine_for(f"sqlite:///{tmp_path}/r.db")
    upgrade(database)
    factory = db.session_factory(database)
    reconciler = Reconciler([], factory, groups=groups())
    with factory() as session:
        row = _deployment("dev", "app")
        session.add(row)
        session.flush()
        reconciler.groups.assign(session)
        assert session.get(db.Deployment, ("dev", "app")).group_name == "Sandbox"
