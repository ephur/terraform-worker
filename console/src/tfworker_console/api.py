"""The read-only JSON API, served under /api/v1 (see /docs for OpenAPI)."""

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from . import queries
from .auth import Principal
from .queries import Scope
from .security import require_admin, require_viewer


def get_session(request: Request):
    sessions = request.app.state.sessions
    if sessions is None:
        raise HTTPException(503, "the console has no database configured")
    with sessions() as session:
        yield session


def get_scope(
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_viewer),
    store: List[str] = Query(default=[], description="Only these stores"),
    deployment: List[str] = Query(default=[], description="Only these deployments"),
    group: List[str] = Query(default=[], description="Only these deployment groups"),
) -> Scope:
    # the screens' pickers send an empty value for "all"
    return Scope(
        stores=[name for name in store if name],
        deployments=[name for name in deployment if name],
        groups=[name for name in group if name],
        resolver=request.app.state.groups,
        allowed=principal.visible_groups(),
    )


def get_now(request: Request) -> datetime:
    clock: Callable[[], datetime] = getattr(
        request.app.state, "clock", lambda: datetime.now(timezone.utc)
    )
    return clock()


def stale_after(request: Request) -> float:
    return request.app.state.settings.lock_stale_after


router = APIRouter(prefix="/api/v1", tags=["read"])


@router.get("/me", tags=["auth"])
def me(principal: Principal = Depends(require_viewer)) -> dict:
    """Who the caller is and the roles they hold."""
    return {
        "name": principal.name,
        "kind": principal.kind,
        "role": principal.highest_role,
        "grants": [grant.as_dict() for grant in principal.grants],
    }


@router.post("/reconcile", tags=["reconcile"])
def reconcile(
    request: Request,
    store: Optional[str] = None,
    principal: Principal = Depends(require_admin),
) -> List[dict]:
    """
    Reconcile every store, or one, now instead of waiting for the schedule.

    It runs before the response returns and is safe to repeat: a store that is
    already reconciling is waited for. It needs the admin role on every group.
    """
    if not principal.allows("admin"):
        raise HTTPException(403, "reconciling needs the admin role on every group")
    reconciler = request.app.state.reconciler
    if reconciler is None:
        raise HTTPException(
            503,
            "nothing to reconcile: the console has no database, or is showing "
            "the demo",
        )
    if store is None:
        return [asdict(summary) for summary in reconciler.reconcile_all()]
    if store not in reconciler.stores:
        raise HTTPException(404, f"no store named {store}")
    return [asdict(reconciler.reconcile(store))]


@router.get("/fleet", response_model=queries.Fleet)
def fleet(
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
):
    """Health counts, and every definition that is not ok, exceptions first."""
    return queries.fleet(session, scope, now)


@router.get("/groups", response_model=List[queries.GroupView])
def groups(
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
):
    """Deployment groups in sort order, with policy, members and health counts."""
    return queries.groups(session, scope, now)


@router.get("/deployments", response_model=List[queries.DeploymentSummary])
def deployments(
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
):
    return queries.deployments(session, scope, now)


@router.get(
    "/definitions/{name:path}/timeline", response_model=List[queries.TimelineEntry]
)
def timeline(
    name: str,
    limit: int = Query(200, ge=1, le=1000),
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
):
    return queries.timeline(session, scope, name, limit)


@router.get("/definitions/{name:path}", response_model=queries.DefinitionView)
def definition(
    name: str,
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
    stale: float = Depends(stale_after),
):
    """A definition across its deployments: health, module skew, plans, lock and runs."""
    view = queries.definition(session, scope, name, now, stale)
    if view is None:
        raise HTTPException(404, f"no definition named {name}")
    return view


@router.get("/plans", response_model=List[queries.PlanView])
def plans(
    status: Optional[str] = None,
    definition: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
):
    return queries.plans(session, scope, now, status, definition, limit)


@router.get("/locks", response_model=List[queries.LockView])
def locks(
    state: Optional[str] = Query(None, pattern="^(live|stale)$"),
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
    stale: float = Depends(stale_after),
):
    return queries.locks(session, scope, now, stale, state)


@router.get("/runs", response_model=List[queries.RunView])
def runs(
    status: Optional[str] = None,
    definition: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
):
    return queries.runs(session, scope, status, definition, limit)


@router.get("/runs/{store}/{record_id}")
def run(
    record_id: str,
    store: str,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_viewer),
) -> dict:
    """The full tfworker.run/v1 record."""
    body = queries.run(session, store, record_id)
    if body is not None:
        row = session.get(queries.db.Run, (store, record_id))
        group = request.app.state.groups.of(store, row.deployment)
        if not principal.allows("viewer", group):
            body = None
    if body is None:
        raise HTTPException(404, f"no run record {record_id} in {store}")
    return body


@router.get("/tracking/local-modules", response_model=List[queries.WeekShare])
def local_modules(
    weeks: int = Query(12, ge=1, le=104),
    session: Session = Depends(get_session),
    scope: Scope = Depends(get_scope),
    now: datetime = Depends(get_now),
):
    """The weekly share of definition applies that used a module from a local path."""
    return queries.local_module_applies(session, scope, now, weeks)
