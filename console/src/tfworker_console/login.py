"""Sign-in and sign-out: local passwords with optional TOTP, and OIDC SSO."""

import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import db
from .auth import AuthError, Principal, local_login, pkce_pair
from .security import remember

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


def _page(request: Request, error: Optional[str] = None, status: int = 200):
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
            "next": _safe_next(request.query_params.get("next")),
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
        with request.app.state.sessions() as session:
            principal = local_login(
                session,
                username,
                password,
                totp,
                sso_enforced=settings.auth == "oidc" and settings.sso_enforced,
                now=_now(request),
            )
    except AuthError as e:
        return _page(request, str(e), status=401)
    remember(request, principal, "local")
    return RedirectResponse(_safe_next(next), status_code=303)


def _redirect_uri(request: Request) -> str:
    public = request.app.state.settings.public_url
    if public:
        return public.rstrip("/") + "/auth/callback"
    return str(request.url_for("auth_callback"))


@router.get("/login/sso")
def login_sso(request: Request):
    oidc = request.app.state.oidc
    if oidc is None:
        raise HTTPException(404, "single sign-on is not configured")
    verifier, challenge = pkce_pair()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    request.session["oidc"] = {
        "state": state,
        "nonce": nonce,
        "verifier": verifier,
        "next": _safe_next(request.query_params.get("next")),
    }
    url = oidc.authorize_url(_redirect_uri(request), state, nonce, challenge)
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
    pending = request.session.pop("oidc", None)
    if error:
        return _page(request, f"the identity provider said: {error}", status=401)
    if not pending or not code or not state or state != pending["state"]:
        return _page(request, "the sign-in expired or was not started here", 400)
    settings = request.app.state.settings
    try:
        claims = oidc.exchange(
            code, pending["verifier"], _redirect_uri(request), pending["nonce"]
        )
    except AuthError as e:
        return _page(request, str(e), status=401)
    name = str(claims.get(settings.oidc_username_claim) or claims["sub"])
    held = claims.get(settings.oidc_groups_claim) or []
    if isinstance(held, str):
        held = [held]
    grants = request.app.state.roles.grants(held)
    if not grants:
        return _page(request, f"{name} has no role in this console", status=403)
    now = _now(request)
    with request.app.state.sessions() as session:
        user = session.get(db.User, name)
        if user is None:
            user = db.User(username=name, source="oidc", created_at=now)
            session.add(user)
        if user.source != "oidc" or user.disabled:
            return _page(request, f"{name} may not sign in with SSO", status=403)
        user.last_login = now
        session.commit()
    remember(request, Principal(name, "user", grants), "oidc")
    return RedirectResponse(pending["next"], status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
