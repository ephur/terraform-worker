"""Server-rendered screens, one per API view, refreshed in place with htmx."""

from datetime import datetime
from importlib import resources
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
    return _render(
        request,
        "plans.html",
        session,
        scope,
        status=status,
        notice=request.session.pop("notice", None),
        plans=queries.plans(session, scope, now, status or None),
    )


def _act(request: Request, page: str, act) -> RedirectResponse:
    """
    Run an action for a screen, then send the browser back to the screen
    with the outcome, so a reload doesn't repeat the action.
    """
    actions = request.app.state.actions
    if actions is None:
        notice = {"ok": False, "text": "the console is not connected to any stores"}
    else:
        try:
            notice = {"ok": True, "text": act(actions).message}
        except ActionError as e:
            notice = {"ok": False, "text": str(e)}
    request.session["notice"] = notice
    target = page + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(target, status_code=303)


# the scope's query parameters are named store and deployment, so the path's
# are named apart from them
@router.post("/plans/{plan_store}/{plan_id}/{action}", response_class=HTMLResponse)
def plan_action(
    plan_store: str,
    plan_id: str,
    action: str,
    request: Request,
    reason: str = Form(""),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
):
    return _act(
        request,
        "/plans",
        lambda actions: actions.plan(
            request.state.principal, plan_store, plan_id, action, now, reason[:1000]
        ),
    )


@router.get("/locks", response_class=HTMLResponse)
def locks_page(
    request: Request,
    state: Optional[str] = Query(None, pattern="^(live|stale)?$"),
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
    stale: float = Depends(stale_after),
):
    return _render(
        request,
        "locks.html",
        session,
        scope,
        state=state,
        notice=request.session.pop("notice", None),
        locks=queries.locks(session, scope, now, stale, state or None),
    )


@router.post(
    "/locks/{lock_store}/{lock_deployment}/{definition:path}/force-unlock",
    response_class=HTMLResponse,
)
def force_unlock(
    lock_store: str,
    lock_deployment: str,
    definition: str,
    request: Request,
    lock_id: str = Form(..., max_length=255),
    confirm: str = Form(""),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
):
    return _act(
        request,
        "/locks",
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
