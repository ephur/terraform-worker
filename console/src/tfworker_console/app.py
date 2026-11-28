import ipaddress
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.middleware.sessions import SessionMiddleware

from . import api, demo, login, metrics, probes, version, web
from .actions import Actions
from .auth import OIDCClient, load_roles
from .db import engine_for, session_factory
from .groups import load_groups
from .ingest import EventConsumer, EventIngest
from .metrics import Metrics, MetricsListener
from .migrate import upgrade
from .reconcile import Reconciler
from .scheduler import ReconcileScheduler
from .security import Forbidden, LoginRequired
from .settings import ConsoleSettings
from .stores import Credentials, load_stores

log = logging.getLogger(__name__)


def _assign_groups(sessions, groups) -> None:
    """Bring every deployment's stored group in line with the groups file."""
    try:
        with sessions() as session:
            groups.assign(session)
    except SQLAlchemyError as e:
        log.warning(f"unable to assign deployment groups (run db upgrade?): {e}")


def _seed_demo(sessions) -> None:
    with sessions() as session:
        try:
            if demo.seed(session, datetime.now(timezone.utc)):
                log.info("seeded the demo data")
        except IntegrityError:
            # another replica seeded it first
            session.rollback()
            demo.is_seeded(session)


def _forget_demo(sessions) -> None:
    """A demo left behind would pass for real data once the flag is off."""
    try:
        with sessions() as session:
            gone = demo.forget(session)
    except SQLAlchemyError:
        return  # no schema yet; nothing to forget
    if gone:
        log.warning(f"removed the demo data left in the database: {', '.join(gone)}")


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
    if not settings.database_url:
        raise ValueError(
            "sign-in keeps accounts in the database; set "
            "TFWORKER_CONSOLE_DATABASE_URL, or TFWORKER_CONSOLE_AUTH=none to develop"
        )
    if settings.auth == "oidc" and not (
        settings.oidc_issuer and settings.oidc_client_id and settings.public_url
    ):
        raise ValueError(
            "TFWORKER_CONSOLE_AUTH=oidc needs TFWORKER_CONSOLE_OIDC_ISSUER, "
            "TFWORKER_CONSOLE_OIDC_CLIENT_ID and TFWORKER_CONSOLE_PUBLIC_URL"
        )


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _cross_origin(request: Request, public_url: Optional[str]) -> bool:
    """
    Whether a browser sent this request from another site. Requests that
    carry a bearer token are not cookie-authenticated and pass; so do
    clients that send neither header.
    """
    if request.headers.get("Authorization", "").lower().startswith("bearer "):
        return False
    if request.headers.get("Sec-Fetch-Site", "same-origin") not in (
        "same-origin",
        "none",
    ):
        return True
    origin = request.headers.get("Origin")
    if origin is None:
        return False
    hosts = {request.headers.get("Host", "")}
    if public_url:
        hosts.add(urlsplit(public_url).netloc)
    return urlsplit(origin).netloc not in hosts


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
        workers = [
            app_state[name]
            for name in ("scheduler", "events", "metrics")
            if name in app_state
        ]
        for worker in workers:
            worker.start()
        yield
        for worker in workers:
            worker.stop()

    app = FastAPI(title="tfworker console", version=version(), lifespan=lifespan)
    app.state.settings = settings
    app.state.sessions = None
    app.state.reconciler = None
    app.state.actions = None
    engine = None
    app.state.groups = load_groups(settings.groups_file)
    if settings.demo:
        if not settings.database_url:
            raise ValueError(
                "TFWORKER_CONSOLE_DEMO needs TFWORKER_CONSOLE_DATABASE_URL"
            )
        if settings.stores_file:
            raise ValueError(
                "TFWORKER_CONSOLE_DEMO shows sample data; leave out "
                "TFWORKER_CONSOLE_STORES_FILE, which would reconcile it away"
            )
        if not settings.groups_file:
            app.state.groups = demo.GROUPS
    if settings.events_queue_url and not settings.database_url:
        raise ValueError(
            "TFWORKER_CONSOLE_EVENTS_QUEUE_URL needs TFWORKER_CONSOLE_DATABASE_URL"
        )
    if settings.database_url:
        engine = engine_for(settings.database_url)
        # the demo starts from nothing, so it always brings the schema up
        if settings.auto_migrate or settings.demo:
            upgrade(engine)
        app.state.sessions = session_factory(engine)
        if settings.demo:
            _seed_demo(app.state.sessions)
        else:
            _forget_demo(app.state.sessions)
        stores = load_stores(settings.stores_file)
        unknown = app.state.groups.unknown_stores(store.name for store in stores)
        if unknown and not settings.demo:
            log.warning(
                f"the groups file names stores that are not configured: "
                f"{', '.join(unknown)}"
            )
        _assign_groups(app.state.sessions, app.state.groups)
        # the demo has no stores to read, and a reconcile would remove its
        # data as belonging to unconfigured stores
        app.state.reconciler = (
            None
            if settings.demo
            else Reconciler(stores, app.state.sessions, groups=app.state.groups)
        )
        if not settings.demo:
            app.state.actions = Actions(
                {store.name: store for store in stores},
                app.state.sessions,
                app.state.groups,
                settings.lock_stale_after,
            )
        if (
            app.state.reconciler is not None
            and settings.reconcile_interval > 0
            and app.state.reconciler.stores
        ):
            app_state["scheduler"] = ReconcileScheduler(
                app.state.reconciler, settings.reconcile_interval
            )
        if settings.events_queue_url:
            if app.state.reconciler is None or not app.state.reconciler.stores:
                raise ValueError(
                    "TFWORKER_CONSOLE_EVENTS_QUEUE_URL needs the stores the events "
                    "come from in TFWORKER_CONSOLE_STORES_FILE"
                )
            app_state["events"] = EventConsumer(
                EventIngest(app.state.reconciler),
                settings.events_queue_url,
                Credentials(role_arn=settings.events_role_arn),
                region=settings.events_region,
            )

    app.state.metrics = None
    if settings.metrics != "off":
        if settings.metrics == "token" and not settings.database_url:
            raise ValueError(
                "TFWORKER_CONSOLE_METRICS=token checks API tokens, which needs "
                "TFWORKER_CONSOLE_DATABASE_URL"
            )
        app.state.metrics = Metrics(
            app.state.sessions,
            app.state.groups,
            settings.lock_stale_after,
            definition_label=settings.metrics_definition_label,
            cache_seconds=settings.metrics_cache_seconds,
        )
        if app.state.reconciler is not None:
            app.state.reconciler.observers.append(app.state.metrics.observe_reconcile)
        if "events" in app_state:
            app_state["events"].observers.append(app.state.metrics.observe_event)
        if settings.metrics == "port":
            app_state["metrics"] = MetricsListener(
                app.state.metrics, settings.metrics_host, settings.metrics_port
            )
    app.state.events = app_state.get("events")
    app.state.readiness = probes.Readiness(engine)
    app.state.roles = load_roles(settings.roles_file)
    for mapping in app.state.roles.role_mappings:
        if mapping.groups is not None:
            mapping.groups = [app.state.groups.canonical(g) for g in mapping.groups]
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
        # login CSRF and cross-site actions: SameSite alone does not cover a
        # sign-in form, which needs no cookie, or sibling subdomains
        if request.method not in SAFE_METHODS and _cross_origin(
            request, settings.public_url
        ):
            response = PlainTextResponse(
                "cross-origin requests are refused", status_code=403
            )
        else:
            response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    if app.state.metrics is not None:
        observe = app.state.metrics.observe_request

        @app.middleware("http")
        async def measure(request: Request, call_next):
            started = time.perf_counter()
            status = 500
            try:
                response = await call_next(request)
                status = response.status_code
                return response
            finally:
                # the route template, so ids in paths never become labels
                route = getattr(request.scope.get("route"), "path", "unmatched")
                observe(route, request.method, status, time.perf_counter() - started)

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"detail": "sign in or send a bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if request.headers.get("HX-Request"):
            # htmx would swap the sign-in page into the content; send the
            # whole browser there, back to the page itself rather than the
            # fragment that was asked for
            current = request.headers.get("HX-Current-URL", "")
            parts = urlsplit(current)
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            return Response(
                headers={"HX-Redirect": f"/login?{urlencode({'next': target})}"}
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

    app.include_router(probes.router)
    app.include_router(metrics.router)
    app.include_router(api.router)
    app.include_router(login.router)
    app.include_router(web.router)

    return app
