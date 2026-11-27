"""Server-rendered screens, one per API view, refreshed in place with htmx."""

from datetime import datetime
from importlib import resources
from typing import Optional, Tuple
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from tfworker.util.duration import format_duration

from . import db, queries
from .actions import ActionError
from .api import get_now, get_scope, get_session, stale_after
from .queries import Scope

templates = Jinja2Templates(
    directory=str(resources.files("tfworker_console") / "templates")
)
templates.env.globals["format_duration"] = format_duration
router = APIRouter(include_in_schema=False)


def _render(request: Request, name: str, session: Session, scope: Scope, **context):
    visible = Scope(resolver=scope.resolver, allowed=scope.allowed)
    deployments = session.execute(
        select(db.Deployment.store, db.Deployment.name)
        .where(*visible.where(db.Deployment, db.Deployment.name))
        .order_by(db.Deployment.store, db.Deployment.name)
    ).all()
    # htmx swaps only the content when the picker or a filter changes, but a
    # history restore needs the whole page back
    partial = (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
    )
    response = templates.TemplateResponse(
        request,
        name,
        {
            "partial": partial,
            "principal": getattr(request.state, "principal", None),
            "dev_mode": request.app.state.settings.auth == "none",
            "actions_enabled": request.app.state.actions is not None,
            "scope": scope,
            "stores": sorted({store for store, _ in deployments}),
            "groups": [
                name
                for name in scope.resolver.names
                if scope.allowed is None or name in scope.allowed
            ],
            "all_deployments": sorted({name for _, name in deployments}),
            # appended to links so the scope follows navigation
            "scope_query": urlencode(
                [("store", s) for s in scope.stores]
                + [("deployment", d) for d in scope.deployments]
                + [("group", g) for g in scope.groups]
            ),
            **context,
        },
    )
    response.headers["Vary"] = "HX-Request"
    return response


@router.get("/", response_class=HTMLResponse)
def fleet_page(
    request: Request,
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
):
    return _render(
        request, "fleet.html", session, scope, fleet=queries.fleet(session, scope, now)
    )


@router.get("/groups", response_class=HTMLResponse)
def groups_page(
    request: Request,
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
):
    return _render(
        request,
        "groups.html",
        session,
        scope,
        group_views=queries.groups(session, scope, now),
    )


@router.get("/deployments", response_class=HTMLResponse)
def deployments_page(
    request: Request,
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
):
    return _render(
        request,
        "deployments.html",
        session,
        scope,
        deployments=queries.deployments(session, scope, now),
    )


@router.get("/definitions/{name:path}", response_class=HTMLResponse)
def definition_page(
    name: str,
    request: Request,
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
    stale: float = Depends(stale_after),
):
    view = queries.definition(session, scope, name, now, stale)
    if view is None:
        raise HTTPException(404, f"no definition named {name}")
    return _render(
        request,
        "definition.html",
        session,
        scope,
        view=view,
        timeline=queries.timeline(session, scope, name, 50),
    )


@router.get("/plans", response_class=HTMLResponse)
def plans_page(
    request: Request,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
):
    return _plans(request, session, scope, now, status)


def _plans(request, session, scope, now, status=None, notice=None, code=200):
    response = _render(
        request,
        "plans.html",
        session,
        scope,
        status=status,
        notice=notice,
        plans=queries.plans(session, scope, now, status or None),
    )
    response.status_code = code
    return response


def _act(request: Request, act) -> Tuple[dict, int]:
    """Run an action for a screen: the notice to show, and the status to answer."""
    actions = request.app.state.actions
    if actions is None:
        return {"ok": False, "text": "the console is not connected to any stores"}, 503
    try:
        result = act(actions)
    except ActionError as e:
        return {"ok": False, "text": str(e)}, e.status
    return {"ok": True, "text": result.message}, 200


# the scope's query parameters are named store and deployment, so the path's
# are named apart from them
@router.post("/plans/{plan_store}/{plan_id}/{action}", response_class=HTMLResponse)
def plan_action(
    plan_store: str,
    plan_id: str,
    action: str,
    request: Request,
    reason: str = Form(""),
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
):
    notice, code = _act(
        request,
        lambda actions: actions.plan(
            request.state.principal, plan_store, plan_id, action, now, reason[:1000]
        ),
    )
    return _plans(request, session, scope, now, notice=notice, code=code)


@router.get("/locks", response_class=HTMLResponse)
def locks_page(
    request: Request,
    state: Optional[str] = Query(None, pattern="^(live|stale)?$"),
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
    stale: float = Depends(stale_after),
):
    return _locks(request, session, scope, now, stale, state)


def _locks(request, session, scope, now, stale, state=None, notice=None, code=200):
    response = _render(
        request,
        "locks.html",
        session,
        scope,
        state=state,
        notice=notice,
        locks=queries.locks(session, scope, now, stale, state or None),
    )
    response.status_code = code
    return response


@router.post(
    "/locks/{lock_store}/{lock_deployment}/{definition:path}/force-unlock",
    response_class=HTMLResponse,
)
def force_unlock(
    lock_store: str,
    lock_deployment: str,
    definition: str,
    request: Request,
    lock_id: str = Form(...),
    confirm: str = Form(""),
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
    stale: float = Depends(stale_after),
):
    notice, code = _act(
        request,
        lambda actions: actions.force_unlock(
            request.state.principal,
            lock_store,
            lock_deployment,
            definition,
            lock_id,
            confirm,
            now,
        ),
    )
    return _locks(request, session, scope, now, stale, notice=notice, code=code)


@router.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
):
    return _render(
        request, "audit.html", session, scope, entries=queries.audit(session, scope)
    )


@router.get("/runs", response_class=HTMLResponse)
def runs_page(
    request: Request,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
):
    return _render(
        request,
        "runs.html",
        session,
        scope,
        status=status,
        runs=queries.runs(session, scope, status or None),
    )


@router.get("/tracking/local-modules", response_class=HTMLResponse)
def tracking_page(
    request: Request,
    weeks: int = Query(12, ge=1, le=104),
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
):
    return _render(
        request,
        "tracking.html",
        session,
        scope,
        weeks=queries.local_module_applies(session, scope, now, weeks),
    )
