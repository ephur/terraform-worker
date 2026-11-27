"""Server-rendered screens, one per API view, refreshed in place with htmx."""

from datetime import datetime
from importlib import resources
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db, queries
from .api import get_now, get_scope, get_session, stale_after
from .queries import Scope

templates = Jinja2Templates(
    directory=str(resources.files("tfworker_console") / "templates")
)
router = APIRouter(include_in_schema=False)


def _render(request: Request, name: str, session: Session, scope: Scope, **context):
    deployments = session.execute(
        select(db.Deployment.store, db.Deployment.name).order_by(
            db.Deployment.store, db.Deployment.name
        )
    ).all()
    # htmx swaps only the content when the picker or a filter changes
    partial = request.headers.get("HX-Request") == "true"
    return templates.TemplateResponse(
        request,
        name,
        {
            "partial": partial,
            "scope": scope,
            "stores": sorted({store for store, _ in deployments}),
            "all_deployments": sorted({name for _, name in deployments}),
            "query": str(request.query_params),
            **context,
        },
    )


@router.get("/", response_class=HTMLResponse)
def fleet_page(
    request: Request,
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
):
    return _render(
        request, "fleet.html", session, scope, fleet=queries.fleet(session, scope)
    )


@router.get("/deployments", response_class=HTMLResponse)
def deployments_page(
    request: Request,
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
):
    return _render(
        request,
        "deployments.html",
        session,
        scope,
        deployments=queries.deployments(session, scope),
    )


@router.get("/definitions/{name}", response_class=HTMLResponse)
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
):
    return _render(
        request,
        "plans.html",
        session,
        scope,
        status=status,
        plans=queries.plans(session, scope, status or None),
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
        locks=queries.locks(session, scope, now, stale, state or None),
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
