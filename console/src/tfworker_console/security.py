"""Request dependencies that identify the caller and check their role."""

from datetime import datetime, timezone
from typing import Callable, Optional

from fastapi import Request

from . import db
from .auth import DEV_PRINCIPAL, Grant, Principal, token_principal, user_grants


class LoginRequired(Exception):
    """No one is signed in; the API answers 401 and the screens redirect."""


class Forbidden(Exception):
    """Signed in, but without the role this needs."""


def _sessions(request: Request):
    sessions = request.app.state.sessions
    if sessions is None:
        from fastapi import HTTPException

        raise HTTPException(503, "the console has no database configured")
    return sessions


def _now(request: Request) -> datetime:
    clock = getattr(request.app.state, "clock", lambda: datetime.now(timezone.utc))
    return clock()


def identify(request: Request) -> Optional[Principal]:
    """The caller, from dev mode, a bearer token or the session cookie."""
    settings = request.app.state.settings
    if settings.auth == "none":
        return DEV_PRINCIPAL
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        with _sessions(request)() as session:
            return token_principal(session, header[7:].strip(), _now(request))
    data = request.session.get("principal")
    if not data:
        return None
    if data["kind"] == "user" and data.get("source") == "local":
        # local roles are read on every request, so a change or a disabled
        # account takes effect at once
        with _sessions(request)() as session:
            user = session.get(db.User, data["name"])
            if user is None or user.disabled:
                request.session.clear()
                return None
            grants = user_grants(session, user.username)
    else:
        grants = [Grant.from_dict(grant) for grant in data["grants"]]
    return Principal(data["name"], data["kind"], grants)


def remember(request: Request, principal: Principal, source: str) -> None:
    request.session.clear()
    request.session["principal"] = {
        "name": principal.name,
        "kind": principal.kind,
        "source": source,
        "grants": [grant.as_dict() for grant in principal.grants],
    }


def require(role: str) -> Callable[..., Principal]:
    """A dependency that needs `role` on at least one deployment group."""

    def dependency(request: Request) -> Principal:
        principal = identify(request)
        if principal is None:
            raise LoginRequired()
        if not principal.allows_somewhere(role):
            raise Forbidden(f"this needs the {role} role")
        request.state.principal = principal
        return principal

    dependency.__name__ = f"require_{role}"
    return dependency


require_viewer = require("viewer")
require_operator = require("operator")
require_admin = require("admin")
