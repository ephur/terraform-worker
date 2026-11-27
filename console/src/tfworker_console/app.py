import ipaddress
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.sessions import SessionMiddleware
from tfworker.report import PLAN_SCHEMA, RUN_SCHEMA, STATUS_SCHEMA

from . import api, login, version, web
from .auth import OIDCClient, load_roles
from .db import engine_for, session_factory
from .groups import load_groups
from .reconcile import Reconciler
from .scheduler import ReconcileScheduler
from .security import Forbidden, LoginRequired
from .settings import ConsoleSettings
from .stores import load_stores

log = logging.getLogger(__name__)


def _assign_groups(sessions, groups) -> None:
    """Bring every deployment's stored group in line with the groups file."""
    try:
        with sessions() as session:
            groups.assign(session)
    except SQLAlchemyError as e:
        log.warning(f"unable to assign deployment groups (run db upgrade?): {e}")


CSP = (
    "default-src 'self'; script-src 'self' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_auth(settings: ConsoleSettings) -> None:
    """Refuse settings that would serve the console without working sign-in."""
    if settings.auth == "none":
        if not _loopback(settings.host) and not settings.auth_allow_remote_none:
            raise ValueError(
                "TFWORKER_CONSOLE_AUTH=none lets anyone act as an admin, so it only "
                "listens on loopback; set TFWORKER_CONSOLE_AUTH_ALLOW_REMOTE_NONE=true "
                "to listen on "
                f"{settings.host} (for example inside a container)"
            )
        return
    if not settings.secret_key:
        raise ValueError("set TFWORKER_CONSOLE_SECRET_KEY to sign session cookies")
    if settings.auth == "oidc" and not (
        settings.oidc_issuer and settings.oidc_client_id
    ):
        raise ValueError(
            "TFWORKER_CONSOLE_AUTH=oidc needs TFWORKER_CONSOLE_OIDC_ISSUER and "
            "TFWORKER_CONSOLE_OIDC_CLIENT_ID"
        )


def create_app(settings: Optional[ConsoleSettings] = None) -> FastAPI:
    settings = settings or ConsoleSettings()
    _check_auth(settings)
    if settings.auth == "none":
        log.warning(
            "authentication is off: everyone who can reach the console is an admin"
        )
    app_state = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = app_state.get("scheduler")
        if scheduler is not None:
            scheduler.start()
        yield
        if scheduler is not None:
            scheduler.stop()

    app = FastAPI(title="tfworker console", version=version(), lifespan=lifespan)
    app.state.settings = settings
    app.state.sessions = None
    app.state.reconciler = None
    app.state.groups = load_groups(settings.groups_file)
    if settings.database_url:
        app.state.sessions = session_factory(engine_for(settings.database_url))
        stores = load_stores(settings.stores_file)
        unknown = app.state.groups.unknown_stores(store.name for store in stores)
        if unknown:
            log.warning(
                f"the groups file names stores that are not configured: "
                f"{', '.join(unknown)}"
            )
        _assign_groups(app.state.sessions, app.state.groups)
        app.state.reconciler = Reconciler(
            stores, app.state.sessions, groups=app.state.groups
        )
        if settings.reconcile_interval > 0 and app.state.reconciler.stores:
            app_state["scheduler"] = ReconcileScheduler(
                app.state.reconciler, settings.reconcile_interval
            )

    app.state.roles = load_roles(settings.roles_file)
    app.state.oidc = (
        OIDCClient(
            settings.oidc_issuer,
            settings.oidc_client_id,
            settings.oidc_client_secret,
            settings.oidc_scopes,
        )
        if settings.auth == "oidc"
        else None
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key or secrets.token_urlsafe(32),
        session_cookie="tfworker_console",
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=settings.secure_cookies and settings.auth != "none",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"detail": "sign in or send a bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(
            f"/login?{urlencode({'next': target})}", status_code=303
        )

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": str(exc)}, status_code=403)
        return PlainTextResponse(str(exc), status_code=403)

    app.include_router(api.router)
    app.include_router(login.router)
    app.include_router(web.router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {
            "status": "ok",
            "version": version(),
            "schemas": [RUN_SCHEMA, STATUS_SCHEMA, PLAN_SCHEMA],
        }

    return app
