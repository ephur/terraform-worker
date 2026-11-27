import base64
import hashlib
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pyotp
import pytest
import yaml
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from test_api import engine  # noqa: F401
from test_api import sessions  # noqa: F401
from test_api import NOW, seed  # noqa: F401

from tfworker_console import db
from tfworker_console.app import create_app
from tfworker_console.auth import (
    AuthError,
    Grant,
    OIDCClient,
    Principal,
    RolesFile,
    check_password,
    hash_password,
    load_roles,
    new_token,
    token_hash,
)
from tfworker_console.cli import main
from tfworker_console.reconcile import Reconciler, StoreSummary
from tfworker_console.settings import ConsoleSettings

GROUPS = {
    "groups": [{"name": "Production"}, {"name": "Sandbox"}],
    "rules": [
        {"store": "dev", "match": "*", "group": "Sandbox"},
        {"match": "*", "group": "Production"},
    ],
}
ISSUER = "https://idp.example.com"
CLIENT_ID = "console"


@pytest.fixture
def files(tmp_path):
    groups = tmp_path / "groups.yaml"
    groups.write_text(yaml.safe_dump(GROUPS))
    roles = tmp_path / "roles.yaml"
    roles.write_text(
        yaml.safe_dump(
            {
                "role_mappings": [
                    {"claim": "tf-admins", "role": "admin"},
                    {"claim": "sandbox-ops", "role": "operator", "groups": ["Sandbox"]},
                ]
            }
        )
    )
    return {"groups": str(groups), "roles": str(roles)}


def settings_for(engine, files, **overrides):  # noqa: F811
    values = dict(
        database_url=engine.url.render_as_string(hide_password=False),
        groups_file=files["groups"],
        roles_file=files["roles"],
        reconcile_interval=0,
        auth="local",
        secret_key="test-secret",
        secure_cookies=False,
    )
    values.update(overrides)
    return ConsoleSettings(**values)


def app_for(engine, sessions, files, **overrides):  # noqa: F811
    app = create_app(settings_for(engine, files, **overrides))
    app.state.sessions = sessions
    app.state.clock = lambda: NOW
    return app


def add_user(sessions, name, role="viewer", groups=None, **fields):  # noqa: F811
    with sessions() as session:
        session.add(
            db.User(
                username=name,
                source=fields.pop("source", "local"),
                password_hash=hash_password(fields.pop("password", "pw")),
                created_at=NOW,
                **fields,
            )
        )
        session.flush()
        if role:
            session.add(db.UserGrant(username=name, role=role, groups=groups))
        session.commit()


def sign_in(client, name, password="pw", totp="", next="/"):
    return client.post(
        "/login",
        data={"username": name, "password": password, "totp": totp, "next": next},
        follow_redirects=False,
    )


@pytest.fixture
def client(engine, sessions, files):  # noqa: F811
    return TestClient(app_for(engine, sessions, files))


class TestSettings:
    def test_dev_mode_only_on_loopback(self, monkeypatch):
        monkeypatch.setenv("TFWORKER_CONSOLE_AUTH", "none")
        create_app(ConsoleSettings(host="::1"))
        create_app(ConsoleSettings(host="localhost"))
        with pytest.raises(ValueError, match="only listens on loopback"):
            create_app(ConsoleSettings(host="0.0.0.0"))
        with pytest.raises(ValueError, match="only listens on loopback"):
            create_app(ConsoleSettings(host="console.internal"))
        create_app(ConsoleSettings(host="0.0.0.0", auth_allow_remote_none=True))

    def test_sign_in_needs_a_secret_and_an_issuer(self):
        with pytest.raises(ValueError, match="SECRET_KEY"):
            create_app(ConsoleSettings(auth="local"))
        with pytest.raises(ValueError, match="OIDC_ISSUER"):
            create_app(ConsoleSettings(auth="oidc", secret_key="x"))

    def test_dev_mode_banner_and_headers(self, engine, sessions, files):  # noqa: F811
        client = TestClient(app_for(engine, sessions, files, auth="none"))
        page = client.get("/")
        assert "Authentication is off" in page.text
        assert "Sign out" not in page.text
        assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
        assert page.headers["X-Content-Type-Options"] == "nosniff"
        assert client.get("/api/v1/me").json()["role"] == "admin"
        assert client.get("/login", follow_redirects=False).status_code == 303
        posted = client.post(
            "/login", data={"username": "a", "password": "b"}, follow_redirects=False
        )
        assert posted.headers["location"] == "/"


class TestSignIn:
    def test_anonymous_is_turned_away(self, client):
        api = client.get("/api/v1/fleet")
        assert api.status_code == 401
        assert api.headers["WWW-Authenticate"] == "Bearer"
        page = client.get("/plans?store=prod", follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"] == "/login?next=%2Fplans%3Fstore%3Dprod"
        assert client.get("/healthz").status_code == 200
        login = client.get("/login?next=/plans")
        assert 'name="next" value="/plans"' in login.text
        assert "Sign in with SSO" not in login.text

    def test_password(self, client, sessions):  # noqa: F811
        add_user(sessions, "ana", "operator")
        response = sign_in(client, "ana", next="/runs")
        assert (response.status_code, response.headers["location"]) == (303, "/runs")
        me = client.get("/api/v1/me").json()
        assert me == {
            "name": "ana",
            "kind": "user",
            "role": "operator",
            "grants": [{"role": "operator", "groups": None}],
        }
        page = client.get("/")
        assert "ana · operator" in page.text and "Sign out" in page.text
        with sessions() as session:
            assert session.get(db.User, "ana").last_login == NOW
        out = client.post("/logout", follow_redirects=False)
        assert out.headers["location"] == "/login"
        assert client.get("/api/v1/fleet").status_code == 401

    @pytest.mark.parametrize(
        "name, password, message",
        [
            ("ana", "wrong", "wrong password"),
            ("nobody", "pw", "wrong password"),
            ("off", "pw", "wrong password"),
            ("sso-person", "pw", "wrong password"),
            ("norole", "pw", "no role"),
        ],
    )
    def test_refused(self, client, sessions, name, password, message):  # noqa: F811
        add_user(sessions, "ana")
        add_user(sessions, "off", disabled=True)
        add_user(sessions, "sso-person", source="oidc")
        add_user(sessions, "norole", role=None)
        response = sign_in(client, name, password)
        assert response.status_code == 401
        assert message in response.text

    def test_one_time_code(self, client, sessions):  # noqa: F811
        secret = pyotp.random_base32()
        add_user(sessions, "ana", totp_secret=secret)
        assert sign_in(client, "ana").status_code == 401
        assert sign_in(client, "ana", totp="000000").status_code == 401
        assert sign_in(client, "ana", totp=pyotp.TOTP(secret).now()).status_code == 303

    @pytest.mark.parametrize(
        "target", ["//evil.example.com/", "https://evil.example.com", "/\\evil", ""]
    )
    def test_redirects_stay_in_the_console(
        self,
        client,
        sessions,  # noqa: F811
        target,
    ):
        add_user(sessions, "ana")
        assert sign_in(client, "ana", next=target).headers["location"] == "/"

    def test_changes_to_an_account_apply_at_once(self, client, sessions):  # noqa: F811
        add_user(sessions, "ana", "viewer")
        sign_in(client, "ana")
        assert client.get("/api/v1/me").json()["role"] == "viewer"
        with sessions() as session:
            session.add(db.UserGrant(username="ana", role="admin", groups=None))
            session.commit()
        assert client.get("/api/v1/me").json()["role"] == "admin"
        with sessions() as session:
            session.get(db.User, "ana").disabled = True
            session.commit()
        assert client.get("/api/v1/me").status_code == 401


class TestRoles:
    def test_group_scoped_viewer_sees_only_their_groups(
        self, client, sessions  # noqa: F811
    ):
        add_user(sessions, "sam", "viewer", ["Sandbox"])
        sign_in(client, "sam")
        deployments = client.get("/api/v1/deployments").json()
        assert [d["deployment"] for d in deployments] == ["sandbox"]
        assert {r["deployment"] for r in client.get("/api/v1/runs").json()} == {
            "sandbox"
        }
        fleet = client.get("/api/v1/fleet").json()
        assert {e["deployment"] for e in fleet["exceptions"]} == {"sandbox"}
        assert client.get("/api/v1/deployments?group=Production").json() == []
        assert client.get("/api/v1/runs/prod/R1").status_code == 404
        assert client.get("/api/v1/runs/dev/R3").status_code == 200
        assert [g["name"] for g in client.get("/api/v1/groups").json()] == ["Sandbox"]
        page = client.get("/").text
        assert "<option >Production</option>" not in page
        assert "<option >alpha</option>" not in page
        assert client.get("/api/v1/definitions/vpc").status_code == 404

    def test_reconcile_needs_admin_on_every_group(
        self, client, sessions, mocker  # noqa: F811
    ):
        mocker.patch.object(
            Reconciler, "reconcile_all", return_value=[StoreSummary(store="prod")]
        )
        add_user(sessions, "vic", "viewer")
        add_user(sessions, "pat", "admin", ["Production"])
        add_user(sessions, "ada", "admin")
        sign_in(client, "vic")
        assert client.post("/api/v1/reconcile").status_code == 403
        assert client.get("/plans").status_code == 200
        sign_in(client, "pat")
        assert client.post("/api/v1/reconcile").status_code == 403
        sign_in(client, "ada")
        assert client.post("/api/v1/reconcile").status_code == 200

    def test_a_page_needing_more_than_the_caller_has(
        self,
        engine,  # noqa: F811
        sessions,  # noqa: F811
        files,
    ):
        from fastapi import Depends

        from tfworker_console.security import require_operator

        app = app_for(engine, sessions, files)

        @app.get("/api/v1/needs-operator")
        def api_needs(principal=Depends(require_operator)):
            return {"ok": True}

        @app.get("/needs-operator")
        def page_needs(principal=Depends(require_operator)):
            return {"ok": True}

        client = TestClient(app)
        add_user(sessions, "vic", "viewer")
        sign_in(client, "vic")
        assert client.get("/api/v1/needs-operator").status_code == 403
        page = client.get("/needs-operator")
        assert page.status_code == 403 and "operator role" in page.text

    def test_grants(self):
        scoped = Principal("p", "user", [Grant("operator", frozenset({"Sandbox"}))])
        assert scoped.allows("viewer", "Sandbox")
        assert not scoped.allows("viewer", "Production")
        assert not scoped.allows("viewer")
        assert not scoped.allows("admin", "Sandbox")
        assert scoped.visible_groups() == {"Sandbox"}
        assert Principal("p", "user").highest_role is None
        everywhere = Principal("p", "user", [Grant("viewer"), Grant("admin", None)])
        assert everywhere.visible_groups() is None
        assert everywhere.highest_role == "admin"


class TestTokens:
    def add(
        self,
        sessions,  # noqa: F811
        name,
        role="viewer",
        groups=None,
        expires_at=None,
    ):
        token = new_token()
        with sessions() as session:
            session.add(
                db.ApiToken(
                    name=name,
                    token_hash=token_hash(token),
                    role=role,
                    groups=groups,
                    created_by="test",
                    created_at=NOW,
                    expires_at=expires_at,
                )
            )
            session.commit()
        return {"Authorization": f"Bearer {token}"}

    def test_bearer_tokens(self, client, sessions):  # noqa: F811
        backstage = self.add(sessions, "backstage", groups=["Sandbox"])
        me = client.get("/api/v1/me", headers=backstage).json()
        assert me["name"] == "token:backstage" and me["kind"] == "token"
        rows = client.get("/api/v1/deployments", headers=backstage).json()
        assert [r["deployment"] for r in rows] == ["sandbox"]
        with sessions() as session:
            assert session.get(db.ApiToken, "backstage").last_used == NOW

    def test_refused_tokens(self, client, sessions):  # noqa: F811
        expired = self.add(sessions, "old", expires_at=NOW - timedelta(seconds=1))
        for headers in (
            expired,
            {"Authorization": "Bearer tfw_not-a-real-token"},
            {"Authorization": "Bearer some-other-kind"},
        ):
            assert client.get("/api/v1/fleet", headers=headers).status_code == 401

    def test_tokens_need_a_database(self, client):
        client.app.state.sessions = None
        headers = {"Authorization": "Bearer tfw_anything"}
        assert client.get("/api/v1/me", headers=headers).status_code == 503


class StubProvider:
    """An OIDC provider that checks PKCE, served through httpx.MockTransport."""

    def __init__(self, groups=("tf-admins",), issuer=ISSUER):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.issuer = issuer
        self.groups = list(groups)
        self.codes = {}
        self.token_status = 200
        self.claims = {}
        self.jwks_fetches = 0

    def jwks(self):
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid=self.kid, use="sig", alg="RS256")
        return {"keys": [jwk]}

    def authorize(self, url):
        params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert params["code_challenge_method"] == "S256"
        assert params["client_id"] == CLIENT_ID
        code = f"code-{len(self.codes)}"
        self.codes[code] = params
        return code, params["state"]

    def id_token(self, params):
        claims = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "sub": "00u1",
            "preferred_username": "rita",
            "groups": self.groups,
            "nonce": params["nonce"],
            "iat": int(NOW.timestamp()),
            "exp": int((NOW + timedelta(hours=1)).timestamp()),
        }
        claims.update(self.claims)
        return jwt.encode(
            claims, self.key, algorithm="RS256", headers={"kid": self.kid}
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/keys",
                },
            )
        if path == "/keys":
            self.jwks_fetches += 1
            return httpx.Response(200, json=self.jwks())
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            params = self.codes.pop(form["code"], None)
            if params is None or self.token_status != 200:
                return httpx.Response(self.token_status if params else 400)
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(form["code_verifier"].encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            assert challenge == params["code_challenge"], "PKCE verifier mismatch"
            assert form["redirect_uri"] == params["redirect_uri"]
            return httpx.Response(200, json={"id_token": self.id_token(params)})
        return httpx.Response(404)  # pragma: no cover


@pytest.fixture
def provider():
    return StubProvider()


@pytest.fixture
def sso(engine, sessions, files, provider):  # noqa: F811
    app = app_for(
        engine,
        sessions,
        files,
        auth="oidc",
        oidc_issuer=ISSUER,
        oidc_client_id=CLIENT_ID,
        oidc_client_secret="s3cret",
        public_url="https://console.example.com/",
    )
    app.state.oidc = OIDCClient(
        ISSUER,
        CLIENT_ID,
        "s3cret",
        "openid groups",
        http=lambda: httpx.Client(transport=httpx.MockTransport(provider.handler)),
        clock=NOW.timestamp,
    )
    return TestClient(app)


def sso_sign_in(client, provider, next="/plans"):
    start = client.get(f"/login/sso?next={next}", follow_redirects=False)
    assert start.status_code == 303
    code, state = provider.authorize(start.headers["location"])
    assert provider.codes[code]["redirect_uri"] == (
        "https://console.example.com/auth/callback"
    )
    return client.get(
        f"/auth/callback?code={code}&state={state}", follow_redirects=False
    )


class TestSSO:
    def test_pkce_flow_and_role_mapping(self, sso, provider, sessions):  # noqa: F811
        assert "Sign in with SSO" in sso.get("/login").text
        done = sso_sign_in(sso, provider)
        assert (done.status_code, done.headers["location"]) == (303, "/plans")
        me = sso.get("/api/v1/me").json()
        assert (me["name"], me["role"]) == ("rita", "admin")
        with sessions() as session:
            user = session.get(db.User, "rita")
            assert (user.source, user.last_login) == ("oidc", NOW)
        # a second sign-in reuses the recorded user, and keys are cached
        sso.post("/logout")
        assert sso_sign_in(sso, provider).status_code == 303
        assert provider.jwks_fetches == 1

    def test_group_scoped_mapping(self, sso, provider):
        provider.groups = ["sandbox-ops", "unmapped"]
        sso_sign_in(sso, provider)
        me = sso.get("/api/v1/me").json()
        assert me["grants"] == [{"role": "operator", "groups": ["Sandbox"]}]
        rows = sso.get("/api/v1/deployments").json()
        assert [r["deployment"] for r in rows] == ["sandbox"]

    def test_a_single_group_claim_string(self, sso, provider):
        provider.claims = {"groups": "tf-admins"}
        assert sso_sign_in(sso, provider).status_code == 303

    @pytest.mark.parametrize(
        "change, status, message",
        [
            ({"groups": ["nothing-mapped"]}, 403, "has no role"),
            ({"nonce": "replayed"}, 401, "does not belong to this sign-in"),
            ({"aud": "someone-else"}, 401, "not valid"),
            ({"exp": int((NOW - timedelta(hours=2)).timestamp())}, 401, "expired"),
            ({"iat": int((NOW + timedelta(hours=2)).timestamp())}, 401, "future"),
        ],
    )
    def test_refused_tokens(self, sso, provider, change, status, message):
        provider.claims = change
        response = sso_sign_in(sso, provider)
        assert response.status_code == status
        assert message in response.text

    def test_refused_callbacks(self, sso, provider, sessions):  # noqa: F811
        # no sign-in was started here
        assert sso.get("/auth/callback?code=x&state=y").status_code == 400
        # the state does not match
        start = sso.get("/login/sso", follow_redirects=False)
        code, _ = provider.authorize(start.headers["location"])
        assert sso.get(f"/auth/callback?code={code}&state=forged").status_code == 400
        # the provider reports an error
        sso.get("/login/sso", follow_redirects=False)
        refused = sso.get("/auth/callback?error=access_denied")
        assert refused.status_code == 401 and "access_denied" in refused.text
        # the token endpoint refuses the code
        provider.token_status = 500
        assert sso_sign_in(sso, provider).status_code == 401
        provider.token_status = 200
        # a local account with the same name cannot be taken over by SSO
        add_user(sessions, "rita")
        taken = sso_sign_in(sso, provider)
        assert taken.status_code == 403 and "may not sign in with SSO" in taken.text

    def test_rotated_keys_are_fetched_again(self, sso, provider):
        sso_sign_in(sso, provider)
        sso.post("/logout")
        provider.kid = "k2"
        provider.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        assert sso_sign_in(sso, provider).status_code == 303
        assert provider.jwks_fetches == 2

    def test_unknown_key_and_wrong_issuer(self, provider):
        client = OIDCClient(
            ISSUER,
            CLIENT_ID,
            None,
            "openid",
            http=lambda: httpx.Client(transport=httpx.MockTransport(provider.handler)),
            clock=NOW.timestamp,
        )
        params = {"nonce": "n"}
        token = provider.id_token(params)
        provider.kid = "other"  # the provider now publishes a different key id
        with pytest.raises(AuthError, match="unknown key"):
            client._key("k1")
        provider.kid = "k1"
        provider.codes["c"] = {"nonce": "n", "redirect_uri": "r", "code_challenge": "x"}
        assert token
        wrong = StubProvider(issuer="https://other.example.com")
        mismatched = OIDCClient(
            ISSUER,
            CLIENT_ID,
            None,
            "openid",
            http=lambda: httpx.Client(transport=httpx.MockTransport(wrong.handler)),
        )
        with pytest.raises(AuthError, match="issuer does not match"):
            mismatched.metadata

    def test_missing_id_token(self, provider):
        def handler(request):
            if request.url.path == "/token":
                return httpx.Response(200, json={"access_token": "a"})
            return provider.handler(request)

        client = OIDCClient(
            ISSUER,
            CLIENT_ID,
            None,
            "openid",
            http=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(AuthError, match="no ID token"):
            client.exchange("c", "v", "r", "n")

    def test_break_glass_only_while_sso_is_enforced(
        self, engine, sessions, files  # noqa: F811
    ):
        app = app_for(
            engine,
            sessions,
            files,
            auth="oidc",
            oidc_issuer=ISSUER,
            oidc_client_id=CLIENT_ID,
            sso_enforced=True,
        )
        client = TestClient(app)
        add_user(sessions, "ana")
        add_user(sessions, "glass", "admin", break_glass=True)
        assert "Break-glass sign-in" in client.get("/login").text
        refused = sign_in(client, "ana")
        assert refused.status_code == 401 and "sign in with SSO" in refused.text
        assert sign_in(client, "glass").status_code == 303

    def test_no_sso_without_oidc(self, client):
        assert client.get("/login/sso").status_code == 404
        assert client.get("/auth/callback?code=x&state=y").status_code == 404

    def test_redirect_uri_without_a_public_url(
        self,
        engine,  # noqa: F811
        sessions,  # noqa: F811
        files,
        provider,
    ):
        app = app_for(
            engine,
            sessions,
            files,
            auth="oidc",
            oidc_issuer=ISSUER,
            oidc_client_id=CLIENT_ID,
        )
        app.state.oidc = OIDCClient(
            ISSUER,
            CLIENT_ID,
            None,
            "openid",
            http=lambda: httpx.Client(transport=httpx.MockTransport(provider.handler)),
            clock=NOW.timestamp,
        )
        start = TestClient(app).get("/login/sso", follow_redirects=False)
        _, _ = provider.authorize(start.headers["location"])
        (params,) = provider.codes.values()
        assert params["redirect_uri"] == "http://testserver/auth/callback"


def test_roles_file(tmp_path):
    assert load_roles(None).grants(["x"]) == []
    path = tmp_path / "roles.yaml"
    path.write_text("")
    assert load_roles(str(path)).role_mappings == []
    with pytest.raises(ValueError):
        RolesFile.model_validate({"role_mappings": [{"claim": "a", "role": "root"}]})


def test_passwords():
    hashed = hash_password("pw")
    assert check_password(hashed, "pw")
    assert not check_password(hashed, "nope")
    assert not check_password("not-a-hash", "pw")
    assert not check_password(None, "pw")


def test_rebuild_keeps_accounts(sessions):  # noqa: F811
    add_user(sessions, "ana")
    Reconciler([], sessions).rebuild()
    with sessions() as session:
        assert session.get(db.User, "ana") is not None
        assert session.get(db.Run, ("prod", "R1")) is None


class TestCli:
    @pytest.fixture
    def env(self, engine, monkeypatch):  # noqa: F811
        monkeypatch.setenv(
            "TFWORKER_CONSOLE_DATABASE_URL",
            engine.url.render_as_string(hide_password=False),
        )
        return CliRunner()

    def run(self, runner, *args, input=None):
        result = runner.invoke(main, list(args), input=input)
        return result

    def test_users(self, env, sessions):  # noqa: F811
        created = self.run(
            env,
            "users",
            "create",
            "root",
            "--role",
            "admin",
            "--break-glass",
            "--totp",
            "--password",
            "pw",
        )
        assert created.exit_code == 0, created.output
        assert "one-time code secret" in created.output
        assert "otpauth://totp/" in created.output
        again = self.run(
            env, "users", "create", "root", "--role", "admin", "--password", "x"
        )
        assert again.exit_code != 0 and "already exists" in again.output
        scoped = self.run(
            env,
            "users",
            "create",
            "sam",
            "--role",
            "viewer",
            "--group",
            "Sandbox",
            input="pw\npw\n",
        )
        assert scoped.exit_code == 0, scoped.output
        granted = self.run(
            env,
            "users",
            "grant",
            "sam",
            "--role",
            "operator",
            "--group",
            "Sandbox",
            "--replace",
        )
        assert granted.exit_code == 0
        assert self.run(env, "users", "grant", "nobody", "--role", "viewer").exit_code
        assert (
            self.run(env, "users", "set-password", "sam", "--password", "new").exit_code
            == 0
        )
        assert self.run(
            env, "users", "set-password", "nobody", "--password", "x"
        ).exit_code
        assert self.run(env, "users", "disable", "sam").exit_code == 0
        assert self.run(env, "users", "disable", "nobody").exit_code
        listed = self.run(env, "users", "list").output
        assert "root\tadmin\tlocal break-glass totp" in listed
        assert "sam\toperator in Sandbox\tlocal disabled" in listed
        with sessions() as session:
            assert check_password(session.get(db.User, "sam").password_hash, "new")

    def test_tokens(self, env, sessions):  # noqa: F811
        made = self.run(
            env,
            "tokens",
            "create",
            "backstage",
            "--role",
            "viewer",
            "--expires-in",
            "90d",
        )
        assert made.exit_code == 0 and made.output.startswith("tfw_")
        with sessions() as session:
            row = session.get(db.ApiToken, "backstage")
            assert row.token_hash == token_hash(made.output.strip())
            assert row.expires_at is not None
        assert self.run(
            env, "tokens", "create", "backstage", "--role", "viewer"
        ).exit_code
        assert (
            self.run(env, "tokens", "create", "ci", "--role", "operator").exit_code == 0
        )
        listed = self.run(env, "tokens", "list").output
        assert "backstage\tviewer\texpires 20" in listed
        assert "ci\toperator\texpires never\tlast used never" in listed
        assert self.run(env, "tokens", "revoke", "backstage").exit_code == 0
        assert self.run(env, "tokens", "revoke", "backstage").exit_code
