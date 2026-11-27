"""Sign-in and sign-out: local passwords with optional TOTP, and OIDC SSO."""

import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import db
from .auth import AuthError, Principal, local_login, pkce_pair
from .security import _sessions, identify, remember, sign_out_everywhere

router = APIRouter(include_in_schema=False)


def _safe_next(target: Optional[str]) -> str:
    """Only redirect within the console after sign-in."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    if "\\" in target:
        return "/"
    return target


def _now(request: Request) -> datetime:
    clock = getattr(request.app.state, "clock", lambda: datetime.now(timezone.utc))
    return clock()


def _page(
    request: Request,
    error: Optional[str] = None,
    status: int = 200,
    next: Optional[str] = None,
):
    from .web import templates

    settings = request.app.state.settings
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "partial": False,
            "sso": settings.auth == "oidc",
            "sso_enforced": settings.auth == "oidc" and settings.sso_enforced,
            "error": error,
            "next": _safe_next(next or request.query_params.get("next")),
        },
        status_code=status,
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.app.state.settings.auth == "none":
        return RedirectResponse("/", status_code=303)
    return _page(request)


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    totp: str = Form(""),
    next: str = Form("/"),
):
    settings = request.app.state.settings
    if settings.auth == "none":
        return RedirectResponse("/", status_code=303)
    try:
        with _sessions(request)() as session:
            principal = local_login(
                session,
                username,
                password,
                totp,
                sso_enforced=settings.auth == "oidc" and settings.sso_enforced,
                now=_now(request),
            )
            version = session.get(db.User, username).session_version
    except AuthError as e:
        return _page(request, str(e), status=401, next=next)
    remember(request, principal, "local", version)
    return RedirectResponse(_safe_next(next), status_code=303)


def _redirect_uri(request: Request) -> str:
    return request.app.state.settings.public_url.rstrip("/") + "/auth/callback"


@router.get("/login/sso")
def login_sso(request: Request):
    oidc = request.app.state.oidc
    if oidc is None:
        raise HTTPException(404, "single sign-on is not configured")
    verifier, challenge = pkce_pair()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    try:
        url = oidc.authorize_url(_redirect_uri(request), state, nonce, challenge)
    except AuthError as e:
        return _page(request, str(e), status=502)
    request.session["oidc"] = {
        "state": state,
        "nonce": nonce,
        "verifier": verifier,
        "next": _safe_next(request.query_params.get("next")),
    }
    return RedirectResponse(url, status_code=303)


@router.get("/auth/callback", name="auth_callback")
def auth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    oidc = request.app.state.oidc
    if oidc is None:
        raise HTTPException(404, "single sign-on is not configured")
    pending = request.session.get("oidc")
    # nothing from the provider is trusted, or the pending sign-in used up,
    # until the state matches the one this browser started
    if not pending or not state or state != pending["state"]:
        return _page(request, "the sign-in expired or was not started here", 400)
    request.session.pop("oidc")
    if error:
        return _page(request, f"the identity provider said: {error}", status=401)
    if not code:
        return _page(request, "the identity provider sent no code", 400)
    settings = request.app.state.settings
    try:
        claims = oidc.exchange(
            code, pending["verifier"], _redirect_uri(request), pending["nonce"]
        )
    except AuthError as e:
        return _page(request, str(e), status=401)
    name = str(claims.get(settings.oidc_username_claim) or claims["sub"])
    subject = f"{claims['iss']}#{claims['sub']}"
    held = claims.get(settings.oidc_groups_claim) or []
    if isinstance(held, str):
        held = [held]
    grants = request.app.state.roles.grants(held)
    if not grants:
        return _page(request, f"{name} has no role in this console", status=403)
    if len(name) > 255 or len(subject) > 1024:
        return _page(request, "the sign-in names an identity that is too long", 403)
    now = _now(request)
    with _sessions(request)() as session:
        # the row belongs to the provider's subject; the name it was first
        # recorded under stays its name, so attribution does not move
        user = session.scalar(select(db.User).where(db.User.subject == subject))
        if user is None:
            if session.get(db.User, name) is not None:
                return _page(request, f"{name} belongs to another account", status=403)
            user = db.User(
                username=name, source="oidc", subject=subject, created_at=now
            )
            session.add(user)
        if user.disabled:
            return _page(request, f"{name} may not sign in", status=403)
        user.last_login = now
        try:
            session.commit()
        except IntegrityError:
            # a first sign-in for the same name won a race
            return _page(request, "the sign-in collided; try again", status=409)
        username, version = user.username, user.session_version
    remember(request, Principal(username, "user", grants), "oidc", version)
    return RedirectResponse(pending["next"], status_code=303)


@router.post("/logout")
def logout(request: Request):
    principal = identify(request)
    if principal is not None and principal.kind == "user":
        # the cookie is the session, so signing out ends every session of
        # the account rather than trusting the browser to forget this one
        with _sessions(request)() as session:
            sign_out_everywhere(session, principal.name)
            session.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
