"""
The `plans` command group: list, discard and sweep the saved plans of a
deployment, through the manifests kept under `<prefix>/_tfworker/plans/`.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Literal, Optional, Sequence, Tuple, get_args

import botocore.exceptions
import click
from pydantic import BaseModel

import tfworker.util.log as log
from tfworker.backends import Backends
from tfworker.commands.base import BaseCommand
from tfworker.exceptions import BackendError
from tfworker.report import DefinitionStatus
from tfworker.report.plans import PlanManifest, PlanStatus
from tfworker.util.plan_manifest import S3ManifestStore, move
from tfworker.util.provenance import current_run_id

PLAN_STATUSES = get_args(PlanStatus)


class SweptPlan(BaseModel):
    """One plan `worker plans gc` retired, or would retire with --dry-run."""

    plan_id: str
    definition: str
    plan_key: str
    record_key: str
    check: Literal["stale", "expired", "orphaned", "applying"]
    status: PlanStatus
    reason: str
    done: bool = False


class PlansCommand(BaseCommand):
    """Work on the saved backend plans of one deployment."""

    def __init__(self, deployment: str, **kwargs) -> None:
        super().__init__(deployment=deployment, **kwargs)
        root = self.app_state.root_options
        auth = self.app_state.authenticators.get("aws")
        if root.backend != Backends.S3 or auth is None:
            log.error("worker plans needs the s3 backend")
            self.ctx.exit(1)
        self._client = auth.backend_session.client("s3")
        self._bucket = root.backend_bucket
        self._prefix = root.backend_prefix
        self.store = S3ManifestStore(self._client, self._bucket, self._prefix)
        self.run_id = current_run_id(self.app_state)[0]

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _manifests(self, keys: Iterable[str]) -> List[Tuple[str, PlanManifest]]:
        found = []
        for key in keys:
            try:
                record = self.store.read(key)
            except BackendError as e:
                log.warn(f"skipping plan record {key}: {e}")
                continue
            if record is not None and record[0] is not None:
                found.append((key, record[0]))
        return found

    def _records(self, definition: Optional[str] = None) -> List[Tuple[str, datetime]]:
        try:
            return self.store.records(definition)
        except BackendError as e:
            log.error(str(e))
            self.ctx.exit(1)

    def list_plans(
        self, statuses: Sequence[str], definition: Optional[str], as_json: bool
    ) -> None:
        plans = [
            manifest
            for _, manifest in self._manifests(
                key for key, _ in self._records(definition)
            )
            if not statuses or manifest.status in statuses
        ]
        plans.sort(key=lambda m: (m.created_at, m.plan_id))
        if as_json:
            click.echo(
                json.dumps(
                    [m.model_dump(mode="json", by_alias=True) for m in plans], indent=2
                )
            )
            return
        if not plans:
            click.echo("no saved plans")
            return
        for m in plans:
            changes = (
                f"+{m.changes.add} ~{m.changes.change} -{m.changes.destroy}"
                if m.changes
                else "-"
            )
            expires = m.expires_at.isoformat() if m.expires_at else "never"
            kind = " destroy" if m.destroy else ""
            click.echo(
                f"{m.plan_id}  {m.definition}  {m.status}{kind}  {changes}  "
                f"created {m.created_at.isoformat()}  expires {expires}  run {m.run_id}"
            )

    def discard(
        self, plan_ids: Sequence[str], reason: str, definition: Optional[str]
    ) -> None:
        """Mark pending plans discarded and remove their plan files."""
        keys = {
            key.rsplit("/", 1)[-1][: -len(".json")]: key
            for key, _ in self._records(definition)
        }
        now = self._now()
        failed = False
        for plan_id in plan_ids:
            key = keys.get(plan_id)
            if key is None:
                log.error(f"no saved plan {plan_id}")
                failed = True
                continue
            try:
                discarded = move(
                    self.store,
                    key,
                    "discarded",
                    now,
                    self.run_id,
                    reason=f"discarded: {reason}",
                    expect={"pending"},
                    plan_id=plan_id,
                )
                record = None if discarded else self.store.read(key)
            except BackendError as e:
                log.error(f"unable to discard plan {plan_id}: {e}")
                failed = True
                continue
            if discarded is None:
                status = record[0].status if record and record[0] else "unreadable"
                log.error(
                    f"plan {plan_id} is {status}; only a pending plan can be discarded"
                )
                failed = True
                continue
            self.store.retire(key, discarded)
            click.echo(f"discarded {plan_id} ({discarded.definition})")
        if failed:
            self.ctx.exit(1)

    def gc(
        self,
        dry_run: bool,
        applying_timeout: Optional[float],
        as_json: bool,
    ) -> None:
        """Retire pending plans that can no longer be applied."""
        now = self._now()
        swept: List[SweptPlan] = []
        try:
            pending = list(self.store.keys_for(None, prune=not dry_run))
        except BackendError as e:
            log.error(str(e))
            self.ctx.exit(1)
        for key, manifest in self._manifests(pending):
            if manifest.status != "pending":
                continue
            try:
                verdict = self._verdict(manifest, now)
            except BackendError as e:
                log.warn(f"unable to check saved plan {manifest.plan_id}: {e}")
                continue
            if verdict is not None:
                check, status, reason = verdict
                swept.append(self._swept(key, manifest, check, status, reason))
        if applying_timeout is not None:
            swept.extend(self._stuck(now, applying_timeout))

        for plan in swept:
            if not dry_run:
                plan.done = self._retire(plan, now)

        if as_json:
            click.echo(json.dumps([p.model_dump(mode="json") for p in swept], indent=2))
            return
        verb = "would retire" if dry_run else "retired"
        for p in swept:
            note = "" if dry_run or p.done else " (it changed first; left alone)"
            click.echo(
                f"{verb} {p.plan_id} ({p.definition}) as {p.status}: {p.reason}{note}"
            )
        if not swept:
            click.echo("no saved plans to retire")

    @staticmethod
    def _swept(key, manifest, check, status, reason) -> SweptPlan:
        return SweptPlan(
            plan_id=manifest.plan_id,
            definition=manifest.definition,
            plan_key=manifest.plan_key,
            record_key=key,
            check=check,
            status=status,
            reason=reason,
        )

    def _retire(self, plan: SweptPlan, now: datetime) -> bool:
        expect = {"applying"} if plan.check == "applying" else {"pending"}
        try:
            moved = move(
                self.store,
                plan.record_key,
                plan.status,
                now,
                self.run_id,
                reason=f"gc: {plan.reason}",
                expect=expect,
                plan_id=plan.plan_id,
            )
        except BackendError as e:
            log.warn(f"unable to retire saved plan {plan.plan_id}: {e}")
            return False
        if moved is None:
            return False
        self.store.retire(plan.record_key, moved)
        return True

    def _verdict(
        self, manifest: PlanManifest, now: datetime
    ) -> Optional[Tuple[str, str, str]]:
        """Why a pending plan should be retired: (check, status, reason), or None."""
        if manifest.definition not in self.app_state.definitions:
            return (
                "orphaned",
                "discarded",
                f"orphaned: {manifest.definition} is no longer in the configuration",
            )
        if manifest.is_expired(now):
            return "expired", "expired", f"expired at {manifest.expires_at.isoformat()}"

        bucket, key = self._state_location(manifest)
        head = self._head(bucket, key)
        if head is None:
            if manifest.base_lineage:
                return "orphaned", "discarded", "orphaned: its state no longer exists"
            return None
        version = head.get("VersionId")
        if manifest.base_state_version_id and version == manifest.base_state_version_id:
            return None
        lineage, serial = self._state_base(bucket, key)
        if (lineage, serial) == (manifest.base_lineage, manifest.base_serial):
            return None
        return "stale", "stale", self._why_changed(manifest)

    def _stuck(self, now: datetime, timeout: float) -> List[SweptPlan]:
        """Plans left applying longer than `timeout` seconds, marked failed."""
        cutoff = now - timedelta(seconds=timeout)
        old = [key for key, modified in self._records() if modified <= cutoff]
        stuck = []
        for key, manifest in self._manifests(old):
            if manifest.status != "applying":
                continue
            since = manifest.history[-1].at if manifest.history else manifest.created_at
            if since <= cutoff:
                stuck.append(
                    self._swept(
                        key,
                        manifest,
                        "applying",
                        "failed",
                        f"applying since {since.isoformat()} by run "
                        f"{manifest.history[-1].run_id if manifest.history else manifest.run_id}",
                    )
                )
        return stuck

    def _state_location(self, manifest: PlanManifest) -> Tuple[str, str]:
        if manifest.state is not None:
            return manifest.state.bucket or self._bucket, manifest.state.state_key
        return self._bucket, f"{self._prefix}/{manifest.definition}/terraform.tfstate"

    def _head(self, bucket: str, key: str) -> Optional[dict]:
        try:
            return self._client.head_object(Bucket=bucket, Key=key)
        except botocore.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise BackendError(f"unable to check state {key}: {e}") from e
        except botocore.exceptions.BotoCoreError as e:
            raise BackendError(f"unable to check state {key}: {e}") from e

    def _state_base(self, bucket: str, key: str) -> Tuple[Optional[str], Optional[int]]:
        try:
            obj = self._client.get_object(Bucket=bucket, Key=key)
            state = json.loads(obj["Body"].read())
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
            ValueError,
        ) as e:
            raise BackendError(f"unable to read state {key}: {e}") from e
        return state.get("lineage") or None, state.get("serial")

    def _why_changed(self, manifest: PlanManifest) -> str:
        """Name the recorded run that moved the state, if there is one."""
        try:
            found = self.app_state.backend.read_report(
                f"status/{manifest.definition}.json"
            )
            status = DefinitionStatus.model_validate_json(found[0]) if found else None
        except (BackendError, ValueError) as e:
            log.debug(f"no run status for {manifest.definition}: {e}")
            status = None
        last = status.last_apply if status else None
        if last is not None and last.at >= manifest.created_at:
            return f"state changed by run {last.run_id}"
        return "state changed outside a recorded run"
