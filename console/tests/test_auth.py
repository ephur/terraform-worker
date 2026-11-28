import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pyotp
import pytest
import yaml
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_api import engine  # noqa: F401
from test_api import sessions  # noqa: F401
from test_api import NOW, seed  # noqa: F401

from tfworker_console import auth, db
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
                    {"claim": "sandbox-ops", "role": "operator", "groups": ["sandbox"]},
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
        with pytest.raises(ValueError, match="DATABASE_URL"):
            create_app(ConsoleSettings(auth="local", secret_key="x"))
        with pytest.raises(ValueError, match="OIDC_ISSUER"):
            create_app(
                ConsoleSettings(auth="oidc", secret_key="x", database_url="sqlite://")
            )
        with pytest.raises(ValueError, match="PUBLIC_URL"):
            create_app(
                ConsoleSettings(
                    auth="oidc",
                    secret_key="x",
                    database_url="sqlite://",
                    oidc_issuer=ISSUER,
                    oidc_client_id=CLIENT_ID,
                )
            )

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
        code = pyotp.TOTP(secret).at(NOW)
        assert sign_in(client, "ana", totp=code).status_code == 303
        # a code works once, and an older one is refused after it
        assert sign_in(client, "ana", totp=code).status_code == 401
        older = pyotp.TOTP(secret).at(NOW - timedelta(seconds=30))
        assert sign_in(client, "ana", totp=older).status_code == 401
        newer = pyotp.TOTP(secret).at(NOW + timedelta(seconds=30))
        assert sign_in(client, "ana", totp=newer).status_code == 303

    def test_a_right_password_with_a_wrong_code_looks_like_any_failure(
        self, client, sessions  # noqa: F811
    ):
        add_user(sessions, "ana", totp_secret=pyotp.random_base32())
        right = sign_in(client, "ana", totp="000000")
        wrong = sign_in(client, "ana", password="nope", totp="000000")
        assert right.status_code == wrong.status_code == 401
        assert (
            "wrong one-time code" in right.text and "wrong one-time code" in wrong.text
        )

    def test_repeated_failures_lock_the_account(self, client, sessions):  # noqa: F811
        add_user(sessions, "ana")
        for _ in range(auth.MAX_FAILED_LOGINS):
            assert sign_in(client, "ana", "wrong").status_code == 401
        # locked: even the right password is refused, with the same message
        locked = sign_in(client, "ana")
        assert locked.status_code == 401 and "wrong password" in locked.text
        with sessions() as session:
            assert session.get(db.User, "ana").locked_until == NOW + auth.LOCKOUT
        client.app.state.clock = lambda: NOW + auth.LOCKOUT
        assert sign_in(client, "ana").status_code == 303
        with sessions() as session:
            user = session.get(db.User, "ana")
            assert (user.failed_logins, user.locked_until) == (0, None)

    def test_a_failed_sign_in_keeps_next(self, client, sessions):  # noqa: F811
        add_user(sessions, "ana")
        page = sign_in(client, "ana", "wrong", next="/plans?group=Sandbox")
        assert 'name="next" value="/plans?group=Sandbox"' in page.text

    def test_signing_out_ends_every_session(
        self, engine, sessions, files  # noqa: F811
    ):
        add_user(sessions, "ana")
        laptop = TestClient(app_for(engine, sessions, files))
        desktop = TestClient(app_for(engine, sessions, files))
        sign_in(laptop, "ana")
        sign_in(desktop, "ana")
        stolen = dict(laptop.cookies)
        laptop.post("/logout")
        assert desktop.get("/api/v1/me").status_code == 401
        replay = TestClient(app_for(engine, sessions, files), cookies=stolen)
        assert replay.get("/api/v1/me").status_code == 401

    def test_cross_origin_posts_are_refused(self, client, sessions):  # noqa: F811
        add_user(sessions, "ana")
        form = {"username": "ana", "password": "pw", "totp": "", "next": "/"}
        for headers in (
            {"Origin": "https://evil.example.com"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
        ):
            response = client.post("/login", data=form, headers=headers)
            assert response.status_code == 403
        same = client.post(
            "/login",
            data=form,
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
            follow_redirects=False,
        )
        assert same.status_code == 303
        # a bearer token is not a cookie, so the browser checks do not apply
        assert (
            client.post(
                "/api/v1/reconcile",
                headers={
                    "Origin": "https://evil.example.com",
                    "Authorization": "Bearer tfw_unknown",
                },
            ).status_code
            == 401
        )

    def test_htmx_is_sent_to_sign_in_as_a_whole_page(self, client):
        response = client.get(
            "/plans",
            headers={
                "HX-Request": "true",
                "HX-Current-URL": "http://testserver/plans?store=prod",
            },
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert response.headers["HX-Redirect"] == "/login?next=%2Fplans%3Fstore%3Dprod"
        bare = client.get("/plans", headers={"HX-Request": "true"})
        assert bare.headers["HX-Redirect"] == "/login?next=%2F"

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
        assert sign_in(client, "ana").status_code == 503


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
        # the provider reports an error for the sign-in this browser started
        start = sso.get("/login/sso", follow_redirects=False)
        _, state = provider.authorize(start.headers["location"])
        refused = sso.get(f"/auth/callback?error=access_denied&state={state}")
        assert refused.status_code == 401 and "access_denied" in refused.text
        # a state with no code
        start = sso.get("/login/sso", follow_redirects=False)
        _, state = provider.authorize(start.headers["location"])
        assert sso.get(f"/auth/callback?state={state}").status_code == 400
        # the token endpoint refuses the code
        provider.token_status = 500
        assert sso_sign_in(sso, provider).status_code == 401
        provider.token_status = 200
        # a local account with the same name cannot be taken over by SSO
        add_user(sessions, "rita")
        taken = sso_sign_in(sso, provider)
        assert taken.status_code == 403 and "belongs to another account" in taken.text

    def test_a_forged_error_neither_shows_nor_cancels(self, sso, provider):
        start = sso.get("/login/sso", follow_redirects=False)
        code, state = provider.authorize(start.headers["location"])
        forged = sso.get("/auth/callback?error=call+0800+now")
        assert forged.status_code == 400 and "0800" not in forged.text
        done = sso.get(
            f"/auth/callback?code={code}&state={state}", follow_redirects=False
        )
        assert done.status_code == 303

    def test_identity_follows_the_subject(self, sso, provider, sessions):  # noqa: F811
        sso_sign_in(sso, provider)
        # the public URL is this console's origin, even behind a proxy
        out = sso.post(
            "/logout",
            headers={"Origin": "https://console.example.com"},
            follow_redirects=False,
        )
        assert out.status_code == 303
        # the provider renames the person: they keep their console name
        provider.claims = {"preferred_username": "rita.renamed"}
        sso_sign_in(sso, provider)
        assert sso.get("/api/v1/me").json()["name"] == "rita"
        sso.post("/logout")
        # someone else whose username is rita does not get rita's row
        provider.claims = {"sub": "00u2"}
        other = sso_sign_in(sso, provider)
        assert other.status_code == 403 and "belongs to another account" in other.text
        provider.claims = {"sub": "00u3", "preferred_username": "x" * 256}
        assert sso_sign_in(sso, provider).status_code == 403
        with sessions() as session:
            assert session.get(db.User, "rita").subject == f"{ISSUER}#00u1"

    def test_a_racing_first_sign_in(
        self, sso, provider, sessions, mocker  # noqa: F811
    ):
        add_user(sessions, "rita", source="oidc", subject="someone#else")
        real_get = Session.get

        def blind(self, entity, ident, **kwargs):
            if entity is db.User:
                return None  # as if the other sign-in had not committed yet
            return real_get(self, entity, ident, **kwargs)  # pragma: no cover

        mocker.patch.object(Session, "get", blind)
        collided = sso_sign_in(sso, provider)
        assert collided.status_code == 409 and "try again" in collided.text

    def test_disabling_an_sso_user_ends_their_session(
        self, sso, provider, sessions  # noqa: F811
    ):
        sso_sign_in(sso, provider)
        assert sso.get("/api/v1/me").status_code == 200
        with sessions() as session:
            session.get(db.User, "rita").disabled = True
            session.commit()
        assert sso.get("/api/v1/me").status_code == 401
        with sessions() as session:
            session.get(db.User, "rita").disabled = False
            session.commit()
        sso.cookies.clear()
        assert sso_sign_in(sso, provider).status_code == 303
        with sessions() as session:
            session.get(db.User, "rita").disabled = True
            session.commit()
        sso.cookies.clear()
        refused = sso_sign_in(sso, provider)
        assert refused.status_code == 403 and "may not sign in" in refused.text

    def test_an_unreachable_provider(self, sso, provider):
        down = {"discovery": True, "token": False}

        def handler(request):
            if down["discovery"] or (down["token"] and request.url.path == "/token"):
                raise httpx.ConnectError("connection refused")
            return provider.handler(request)

        sso.app.state.oidc._http = lambda: httpx.Client(
            transport=httpx.MockTransport(handler)
        )
        page = sso.get("/login/sso")
        assert page.status_code == 502 and "could not reach" in page.text
        down.update(discovery=False, token=True)
        failed = sso_sign_in(sso, provider)
        assert failed.status_code == 401 and "could not reach" in failed.text

    def test_a_token_response_that_is_not_json(self, provider):
        def handler(request):
            if request.url.path == "/token":
                return httpx.Response(200, text="<html>")
            return provider.handler(request)

        client = OIDCClient(
            ISSUER,
            CLIENT_ID,
            None,
            "openid",
            http=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(AuthError, match="could not reach"):
            client.exchange("c", "v", "r", "n")

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
            public_url="https://console.example.com",
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


def test_roles_file_groups_must_exist(engine, sessions, files, tmp_path):  # noqa: F811
    roles = tmp_path / "bad-roles.yaml"
    roles.write_text(
        yaml.safe_dump(
            {"role_mappings": [{"claim": "a", "role": "viewer", "groups": ["Nope"]}]}
        )
    )
    with pytest.raises(ValueError, match="no deployment group named 'Nope'"):
        app_for(engine, sessions, {**files, "roles": str(roles)})


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
    def env(self, engine, files, monkeypatch):  # noqa: F811
        monkeypatch.setenv(
            "TFWORKER_CONSOLE_DATABASE_URL",
            engine.url.render_as_string(hide_password=False),
        )
        monkeypatch.setenv("TFWORKER_CONSOLE_GROUPS_FILE", files["groups"])
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
            "sandbox",
            "--replace",
        )
        assert granted.exit_code == 0
        unknown = self.run(
            env, "users", "grant", "sam", "--role", "viewer", "--group", "Nope"
        )
        assert (
            unknown.exit_code and "no deployment group named 'Nope'" in unknown.output
        )
        assert self.run(env, "users", "grant", "nobody", "--role", "viewer").exit_code
        assert (
            self.run(env, "users", "set-password", "sam", "--password", "new").exit_code
            == 0
        )
        assert self.run(
            env, "users", "set-password", "nobody", "--password", "x"
        ).exit_code
        with sessions() as session:
            assert check_password(session.get(db.User, "sam").password_hash, "new")
            assert session.get(db.User, "sam").session_version == 1
            session.get(db.User, "root").locked_until = datetime.now(
                timezone.utc
            ) + timedelta(minutes=5)
            session.commit()
        assert self.run(env, "users", "sign-out", "sam").exit_code == 0
        assert self.run(env, "users", "sign-out", "nobody").exit_code
        assert self.run(env, "users", "disable", "sam").exit_code == 0
        assert self.run(env, "users", "disable", "nobody").exit_code
        listed = self.run(env, "users", "list").output
        assert "root\tadmin\tlocal break-glass totp locked" in listed
        assert "sam\toperator in Sandbox\tlocal disabled" in listed
        with sessions() as session:
            assert session.get(db.User, "sam").session_version == 2
        assert (
            self.run(env, "users", "set-password", "root", "--password", "x").exit_code
            == 0
        )
        with sessions() as session:
            assert session.get(db.User, "root").locked_until is None

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
            self.run(
                env,
                "tokens",
                "create",
                "ci",
                "--role",
                "operator",
                "--group",
                "SANDBOX",
            ).exit_code
            == 0
        )
        with sessions() as session:
            assert session.get(db.ApiToken, "ci").groups == ["Sandbox"]
        listed = self.run(env, "tokens", "list").output
        assert "backstage\tviewer\texpires 20" in listed
        assert "ci\toperator\texpires never\tlast used never" in listed
        for bad, message in (("soon", "invalid duration"), ("0", "longer than zero")):
            refused = self.run(
                env, "tokens", "create", "x", "--role", "viewer", "--expires-in", bad
            )
            assert refused.exit_code == 2 and message in refused.output
        assert self.run(env, "tokens", "revoke", "backstage").exit_code == 0
        assert self.run(env, "tokens", "revoke", "backstage").exit_code
