"""
Admin actions on saved plans and state locks, each written to the audit log.

Operators can re-check a pending plan against its state, expire a plan past
its TTL, and discard a plan with a reason. Plans move with the worker's own
conditional writes (``tfworker.util.plan_manifest.move``), so a run taking
the plan at the same moment wins or loses cleanly, and the move lands in the
manifest's history with who made it and why.

A state lock can be force-unlocked only when it is provably dead: its
holder's worker lease stopped beating, or it has no lease and is older than
``lock_stale_after``. The lock is read again from the store, never judged
from the projection, and deleted only if it is still the same object. The
group's ``force_unlock`` policy says whether operators or only admins may do
it, and the caller must type the definition's name.

The console writes only plan manifests and their index entries under
``_tfworker/``, the retired plan's file and log, the ``.tflock`` object and
the dead holder's lease object. State is read only to re-check a plan.

Every attempt is audited: done, unchanged, refused, forbidden or failed.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import quote

import botocore.exceptions
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from tfworker.backends.lock import LockInfo, WorkerLease
from tfworker.exceptions import BackendError
from tfworker.report import PlanManifest
from tfworker.util.plan_manifest import S3ManifestStore, move

from . import db
from .auth import Principal
from .groups import Groups
from .reconcile import LEASE_SUFFIX, LOCK_SUFFIX, STATE_FILE
from .stores import StoreConfig

log = logging.getLogger(__name__)

PLAN_ACTIONS = ("recheck", "expire", "discard")
MISSING = ("NoSuchKey", "404", "NotFound")
CONFLICT = ("PreconditionFailed", "412", "ConditionalRequestConflict", "409")
CLIENT_ERRORS = (
    BackendError,
    botocore.exceptions.ClientError,
    botocore.exceptions.BotoCoreError,
)
TEXT = 2048


class ActionError(Exception):
    """An action that did not happen; `status` is the HTTP status to answer."""

    status = 409
    outcome = "refused"


class NotFound(ActionError):
    status = 404


class Invalid(ActionError):
    status = 400


class Refused(ActionError):
    status = 409


class Denied(ActionError):
    status = 403
    outcome = "forbidden"


class Failed(ActionError):
    status = 502
    outcome = "failed"


@dataclass
class ActionResult:
    action: str
    # done, or unchanged when a re-check found nothing to do
    outcome: str
    message: str
    # the plan's status afterwards, for plan actions
    status: Optional[str] = None


@dataclass
class _Target:
    store: str
    deployment: str
    definition: str
    group: str
    target: str


class Actions:
    def __init__(
        self,
        stores: Dict[str, StoreConfig],
        sessions: sessionmaker,
        groups: Groups,
        stale_after: float,
        client_for: Optional[Callable[[StoreConfig], object]] = None,
    ):
        self.stores = stores
        self.sessions = sessions
        self.groups = groups
        self.stale_after = stale_after
        self._client_for = client_for or (lambda store: store.session().client("s3"))

    # plans

    def plan(
        self,
        principal: Principal,
        store: str,
        plan_id: str,
        action: str,
        now: datetime,
        reason: str = "",
    ) -> ActionResult:
        if action not in PLAN_ACTIONS:
            raise NotFound(f"no plan action named {action}")
        with self.sessions() as session:
            row = session.get(db.Plan, (store, plan_id))
            target = row and self._target(store, row.deployment, row.definition)
        if row is None or not principal.allows("viewer", target.group):
            raise NotFound(f"no plan {plan_id} in {store}")
        target.target = plan_id
        reason = " ".join(reason.split())
        return self._audited(
            principal,
            f"plan.{action}",
            target,
            reason,
            now,
            lambda: self._plan(principal, target, action, reason, now),
        )

    def _plan(self, principal, target, action, reason, now) -> ActionResult:
        if not principal.allows("operator", target.group):
            raise Denied(f"this needs the operator role on {target.group}")
        if action == "discard" and not reason:
            raise Invalid("give a reason for discarding the plan")
        config = self._store(target.store)
        client = self._client_for(config)
        plans = S3ManifestStore(client, config.bucket, config.render(target.deployment))
        key = plans.record_key(target.definition, target.target)
        found = plans.read(key)
        if found is None or found[0] is None:
            raise Refused(f"the plan's manifest {key} is gone or unreadable")
        manifest = found[0]
        if manifest.status != "pending":
            self._project_plan(target.store, manifest)
            raise Refused(f"the plan is {manifest.status}, not pending")

        if action == "recheck":
            verdict = self._verdict(client, config, target, manifest, now)
            if verdict is None:
                return ActionResult(
                    action,
                    "unchanged",
                    "the plan still matches its state and can be applied",
                    "pending",
                )
            status, why = verdict
            note = f"re-checked by {principal.name}: {why}"
        elif action == "expire":
            why = self._past_ttl(target, manifest, now)
            if why is None:
                raise Refused(
                    "the plan is within its expiry and its group's plan TTL; "
                    "discard it instead"
                )
            status = "expired"
            note = f"expired by {principal.name}: {reason or why}"
        else:
            status = "discarded"
            note = f"discarded by {principal.name}: {reason}"

        moved = move(
            plans,
            key,
            status,
            now,
            reason=f"console: {note}",
            expect={"pending"},
            plan_id=target.target,
        )
        if moved is None:
            raise Refused("the plan changed while acting on it; reload and try again")
        message = f"the plan is now {status}: {note}"
        if self._owns(config, target, moved.plan_key, ".tfplan"):
            plans.retire(key, moved)
        else:
            # a manifest names its plan file; never delete outside the deployment
            log.warning(f"not removing {moved.plan_key}: outside {target.deployment}")
            message += f"; its plan file {moved.plan_key} was left alone"
        self._project_plan(target.store, moved)
        return ActionResult(action, "done", message, status)

    @staticmethod
    def _owns(config: StoreConfig, target, key: str, suffix: str) -> bool:
        """Whether `key` is a `suffix` object of this definition in this deployment."""
        base = f"{config.render(target.deployment)}/{target.definition}/"
        return key.startswith(base) and key.endswith(suffix) and ".." not in key

    def _past_ttl(self, target, manifest: PlanManifest, now) -> Optional[str]:
        if manifest.is_expired(now):
            return f"expired at {manifest.expires_at.isoformat()}"
        ttl = self.groups.group(target.group).plan_ttl
        if ttl is not None and manifest.storage != "preview":
            if now >= manifest.created_at + timedelta(seconds=ttl):
                return f"older than the {target.group} plan TTL"
        return None

    def _verdict(
        self, client, config: StoreConfig, target, manifest, now
    ) -> Optional[Tuple[str, str]]:
        """What a re-check finds: (status, why) to retire the plan, or None."""
        if manifest.is_expired(now):
            return "expired", f"expired at {manifest.expires_at.isoformat()}"
        # only the definition's own state, whatever the manifest says
        bucket = config.bucket
        key = f"{config.render(target.deployment)}/{target.definition}/{STATE_FILE}"
        named = manifest.state
        if named is not None and (
            named.state_key != key or (named.bucket or bucket) != bucket
        ):
            raise Refused(
                f"the manifest names state {named.state_key} outside "
                f"{target.deployment}; discard the plan instead"
            )
        try:
            head = client.head_object(Bucket=bucket, Key=key)
        except botocore.exceptions.ClientError as e:
            if _code(e) not in MISSING:
                raise
            if manifest.base_lineage:
                return "stale", "its state no longer exists"
            return None
        version = head.get("VersionId")
        if manifest.base_state_version_id and version == manifest.base_state_version_id:
            return None
        try:
            state = json.loads(client.get_object(Bucket=bucket, Key=key)["Body"].read())
        except ValueError as e:
            raise BackendError(f"state {key} is not readable: {e}") from e
        if not isinstance(state, dict):
            raise BackendError(f"state {key} is not a terraform state")
        lineage, serial = state.get("lineage") or None, state.get("serial")
        if (lineage, serial) == (manifest.base_lineage, manifest.base_serial):
            return None
        if lineage != manifest.base_lineage:
            return "stale", "the state was replaced (its lineage changed)"
        return (
            "stale",
            f"the state moved from serial {manifest.base_serial} to {serial}",
        )

    def _project_plan(self, store: str, manifest: PlanManifest) -> None:
        """
        Show the plan's new status now rather than after the next reconcile,
        which catches up if this fails.
        """
        try:
            with self.sessions() as session:
                row = session.get(db.Plan, (store, manifest.plan_id))
                if row is not None:
                    row.status = manifest.status
                    row.body = manifest.model_dump(mode="json", by_alias=True)
                    session.commit()
        except SQLAlchemyError as e:
            log.warning(f"unable to project plan {manifest.plan_id} now: {e}")

    # locks

    def force_unlock(
        self,
        principal: Principal,
        store: str,
        deployment: str,
        definition: str,
        lock_id: str,
        confirm: str,
        now: datetime,
    ) -> ActionResult:
        target = self._target(store, deployment, definition)
        with self.sessions() as session:
            row = session.get(db.Lock, (store, deployment, definition))
        if row is None or not principal.allows("viewer", target.group):
            raise NotFound(f"no lock on {definition} in {store}/{deployment}")
        target.target = lock_id
        return self._audited(
            principal,
            "lock.force_unlock",
            target,
            None,
            now,
            lambda: self._force_unlock(principal, target, lock_id, confirm, now),
        )

    def _force_unlock(self, principal, target, lock_id, confirm, now) -> ActionResult:
        policy = self.groups.group(target.group)
        role = "operator" if policy.force_unlock == "operators" else "admin"
        if not principal.allows(role, target.group):
            raise Denied(
                f"force-unlocking in {target.group} needs the {role} role there"
            )
        if confirm.strip() != target.definition:
            raise Invalid(f"type {target.definition} to confirm the force-unlock")
        config = self._store(target.store)
        client = self._client_for(config)
        state_key = (
            f"{config.render(target.deployment)}/{target.definition}/{STATE_FILE}"
        )
        lock_key = state_key + LOCK_SUFFIX
        try:
            obj = client.get_object(Bucket=config.bucket, Key=lock_key)
        except botocore.exceptions.ClientError as e:
            if _code(e) not in MISSING:
                raise
            self._forget_lock(target)
            raise Refused("the lock was already released")
        try:
            lock = LockInfo.model_validate_json(obj["Body"].read())
        except ValueError as e:
            raise Refused(
                f"the lock object is not a terraform lock, so it can't be judged: {e}"
            )
        if lock.id != lock_id:
            raise Refused(
                f"the lock changed: it is now {lock.id}, held by {lock.who or 'unknown'}; "
                "reload and look again"
            )
        lease_key, lease, lease_tag = self._lease(
            client, config.bucket, state_key, lock
        )
        if lease is not None and lease.holds(lock):
            if lease.is_fresh(now):
                raise Refused(
                    f"the lock is live: its holder's lease beat at "
                    f"{lease.heartbeat_at.isoformat()}"
                )
            why = f"its holder's lease last beat at {lease.heartbeat_at.isoformat()}"
        else:
            lease = None
            if lock.age(now) <= self.stale_after:
                raise Refused(
                    "the lock may be live: it has no worker lease and is younger "
                    f"than {int(self.stale_after)}s"
                )
            why = f"it has no worker lease and was taken at {lock.created.isoformat()}"
        try:
            client.delete_object(
                Bucket=config.bucket, Key=lock_key, IfMatch=obj["ETag"]
            )
        except botocore.exceptions.ClientError as e:
            if _code(e) in CONFLICT or _code(e) in MISSING:
                raise Refused(
                    "the lock changed while removing it; reload and look again"
                )
            raise
        held = f"held by {lock.who or 'unknown'}" + (
            f" for {lock.operation}" if lock.operation else ""
        )
        message = f"removed the lock {held}: {why}"
        if lease is not None:
            # the lock is gone either way; a lease left behind is harmless
            try:
                client.delete_object(
                    Bucket=config.bucket, Key=lease_key, IfMatch=lease_tag
                )
            except CLIENT_ERRORS as e:
                log.warning(f"unable to remove the dead lease {lease_key}: {e}")
                message += f"; its dead lease could not be removed ({_describe(e)})"
        self._forget_lock(target)
        return ActionResult("force_unlock", "done", message)

    @staticmethod
    def _lease(
        client, bucket, state_key, lock
    ) -> Tuple[str, Optional[WorkerLease], Optional[str]]:
        """The holder's lease key, the lease if readable, and its ETag."""
        key = f"{state_key}{LEASE_SUFFIX}{quote(lock.host, safe='')}"
        if not lock.host:
            return key, None, None
        try:
            obj = client.get_object(Bucket=bucket, Key=key)
        except botocore.exceptions.ClientError as e:
            if _code(e) not in MISSING:
                raise
            return key, None, None
        try:
            lease = WorkerLease.model_validate_json(obj["Body"].read())
        except ValueError as e:
            log.warning(f"ignoring unreadable worker lease {key}: {e}")
            return key, None, None
        return key, lease, obj["ETag"]

    def _forget_lock(self, target: _Target) -> None:
        """Drop the lock from the projection now; the next reconcile would too."""
        try:
            with self.sessions() as session:
                row = session.get(
                    db.Lock, (target.store, target.deployment, target.definition)
                )
                if row is not None:
                    session.delete(row)
                    session.commit()
        except SQLAlchemyError as e:
            log.warning(f"unable to drop the lock on {target.definition} now: {e}")

    # shared

    def _target(self, store: str, deployment: str, definition: str) -> _Target:
        return _Target(
            store, deployment, definition, self.groups.of(store, deployment), ""
        )

    def _store(self, name: str) -> StoreConfig:
        config = self.stores.get(name)
        if config is None:
            raise Refused(f"store {name} is not configured, so it can't be changed")
        return config

    def _audited(self, principal, action, target, reason, now, act) -> ActionResult:
        """
        Run an action between two audit writes: the entry is recorded as
        started before the store is touched, so an action that happens is
        never missing from the log, and then given its outcome.
        """
        entry = self._audit_start(principal, action, target, reason, now)
        try:
            result = act()
        except ActionError as e:
            self._audit_end(entry, e.outcome, str(e))
            raise
        except CLIENT_ERRORS as e:
            log.warning(f"{action} on {target.target} failed: {e}")
            message = f"the store could not be changed: {_describe(e)}"
            self._audit_end(entry, "failed", message)
            raise Failed(message) from e
        except Exception as e:
            log.exception(f"{action} on {target.target} failed: {e}")
            message = f"the action failed: {e.__class__.__name__}"
            self._audit_end(entry, "failed", message)
            raise Failed(message) from e
        self._audit_end(entry, result.outcome, result.message)
        return result

    def _audit_start(self, principal, action, target, reason, now) -> int:
        with self.sessions() as session:
            entry = db.AuditEntry(
                at=now,
                actor=principal.name[:255],
                actor_kind=principal.kind,
                action=action,
                store=target.store[:128],
                deployment=target.deployment[:255],
                definition=target.definition[:255],
                group_name=target.group[:64],
                target=target.target[:255],
                outcome="started",
                reason=(reason or None) and reason[:TEXT],
            )
            session.add(entry)
            session.commit()
            return entry.id

    def _audit_end(self, entry: int, outcome: str, detail: str) -> None:
        with self.sessions() as session:
            row = session.get(db.AuditEntry, entry)
            row.outcome = outcome
            row.detail = detail[:TEXT]
            session.commit()


def _describe(error: Exception) -> str:
    if isinstance(error, botocore.exceptions.ClientError):
        return f"{_code(error)}: {error}"
    return str(error)


def _code(error: botocore.exceptions.ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))
