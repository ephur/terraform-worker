import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_api import engine  # noqa: F401
from test_api import sessions  # noqa: F401
from test_api import NOW, seed  # noqa: F401  (fixtures below reuse the seed)

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
        assert (
            groups({"groups": [{"name": "A", "plan_ttl": 90}]}).group("A").plan_ttl
            == 90
        )

    @pytest.mark.parametrize(
        "config, message",
        [
            ({"groups": [{"name": UNASSIGNED}]}, "built in"),
            ({"groups": [{"name": "A"}, {"name": "A"}]}, "duplicate groups: A"),
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
    settings = ConsoleSettings(groups_file=str(path), reconcile_interval=0)
    app = create_app(settings)
    app.state.sessions = sessions
    app.state.clock = lambda: NOW
    return TestClient(app)


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
        # Production's 1h plan TTL has expired P1, so beta-us/net is no longer
        # pending, only locked; within locked, Production comes before Sandbox
        locked = [
            (e["group"], e["deployment"]) for e in exceptions if e["health"] == "locked"
        ]
        assert locked == [
            ("Production", "alpha"),
            ("Production", "beta-us"),
            ("Sandbox", "sandbox"),
        ]

    def test_scope_by_group(self, client):
        body = client.get("/api/v1/fleet?group=Sandbox").json()
        assert {e["deployment"] for e in body["exceptions"]} == {"sandbox"}
        assert sum(body["counts"].values()) == 1
        plans = client.get("/api/v1/plans?group=Production").json()
        assert sorted(p["plan_id"] for p in plans) == ["P1", "P2"]
        both = client.get("/api/v1/deployments?group=Production&group=Sandbox")
        assert len(both.json()) == 3
        assert client.get(f"/api/v1/runs?group={UNASSIGNED}").json() == []
        assert client.get("/api/v1/locks?group=nope").json() == []
        assert client.get("/api/v1/fleet?group=").json()["counts"]["failing"] == 1

    def test_group_and_store_scopes_combine(self, client):
        rows = client.get("/api/v1/deployments?group=Production&store=dev").json()
        assert rows == []

    def test_plan_ttl_is_applied_per_group(self, client):
        plans = {p["plan_id"]: p for p in client.get("/api/v1/plans").json()}
        # P1 is pending in Production, created 2h ago with a 1h TTL
        assert plans["P1"]["expired"] is True
        assert plans["P1"]["expires_at"].startswith("2026-11-27T11:00")
        # P2 is a preview in Production, created 1h ago: exactly at the TTL
        assert plans["P2"]["expired"] is True
        # P3 is in Sandbox with no TTL, and discarded anyway
        assert plans["P3"]["expired"] is False
        assert plans["P3"]["expires_at"].startswith("2026-11-28T09:00")

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

    def test_screens(self, client):
        page = client.get("/groups").text
        assert "1h00m" in page and "prod/beta-us" in page
        fleet = client.get("/?group=Sandbox").text
        assert "<option selected>Sandbox</option>" in fleet
        assert "expired" in client.get("/plans").text
        assert "unlock: operators" in client.get("/locks").text


def test_without_groups_everything_is_unassigned(sessions):  # noqa: F811
    with sessions() as session:
        rows = queries.deployments(session, Scope().resolve(session), NOW)
        assert {row.group for row in rows} == {UNASSIGNED}
        plans = queries.plans(session, Scope(), NOW)
        assert not any(plan.expired for plan in plans)
