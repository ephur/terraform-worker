"""Fold one run's result for a definition into that definition's status."""

from datetime import datetime, timezone
from typing import Optional

from .models import DefinitionResult, DefinitionStatus, PhaseResult, RunEvent, RunRecord


def _event(run: RunRecord, result: DefinitionResult, phase: PhaseResult) -> RunEvent:
    planned = phase.action == "plan"
    return RunEvent(
        run_id=run.run_id,
        action=phase.action,
        status=phase.status,
        at=phase.finished_at or run.finished_at or run.started_at,
        duration_seconds=phase.duration_seconds,
        changes=result.changes.counts_only() if planned and result.changes else None,
        base_lineage=result.base_lineage if planned else None,
        base_serial=result.base_serial if planned else None,
        applied_serial=None if planned else result.applied_serial,
        plan_storage=run.provenance.plan_storage if planned else None,
        reason=phase.reason,
        tags=list(result.tags),
        labels=dict(run.provenance.labels),
    )


def _newer(event: RunEvent, current: Optional[RunEvent]) -> bool:
    return current is None or event.at >= current.at


def merge_status(
    current: Optional[DefinitionStatus],
    run: RunRecord,
    result: DefinitionResult,
    now: datetime,
) -> DefinitionStatus:
    """
    Return the status after `result`, never moving a field back to an older event.

    A pending plan is a successful plan with changes, stored in the backend,
    from a run that did not mean it as a preview or drift check. A later
    successful apply or destroy clears it, because the plan no longer matches
    the state. A drift check sets or clears drift.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    status = (
        current.model_copy(deep=True)
        if current is not None
        else DefinitionStatus(
            deployment=run.deployment,
            definition=result.definition,
            state=result.state,
            updated_at=now,
            updated_by_run=run.run_id,
        )
    )
    status.state = result.state
    status.updated_at = now
    status.updated_by_run = run.run_id
    intent = run.provenance.intent

    for phase in result.phases.values():
        if phase.status == "skipped":
            continue
        event = _event(run, result, phase)
        if phase.status == "failed":
            if _newer(event, status.last_failure):
                status.last_failure = event
        if phase.action == "plan":
            if _newer(event, status.last_plan):
                status.last_plan = event
            if phase.status != "succeeded":
                continue
            changed = bool(result.changes and result.changes.total)
            if intent == "drift":
                if _newer(event, status.drift):
                    status.drift = event if changed else None
            elif (
                changed
                and intent != "preview"
                and run.provenance.plan_storage == "backend"
                and _newer(event, status.pending_plan)
            ):
                status.pending_plan = event
        elif phase.action in ("apply", "destroy"):
            if _newer(event, status.last_apply):
                status.last_apply = event
            if phase.status == "succeeded":
                if status.pending_plan and event.at >= status.pending_plan.at:
                    status.pending_plan = None
                if status.drift and event.at >= status.drift.at:
                    status.drift = None
    return status
