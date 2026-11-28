"""
Read, write and move plan manifests.

In the backend each plan's manifest is kept at its own key,
`<prefix>/_tfworker/plans/<definition>/<plan_id>.json`, so its history
survives a newer plan in the same slot; the slot holds a small pointer,
`<plan key>.manifest.json`, naming the current one. On disk the manifest sits
beside the plan file.
"""

import fcntl
import hashlib
import json
import os
from abc import ABC, abstractmethod
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple
from zipfile import BadZipFile, ZipFile

import botocore.exceptions

import tfworker.report.events as events
import tfworker.util.log as log
from tfworker.exceptions import BackendError, ReportConflictError
from tfworker.report import ChangeSummary, new_ulid
from tfworker.report.events import PLAN_EVENT
from tfworker.report.plans import PlanManifest

MANIFEST_SUFFIX = ".manifest.json"
PLAN_NAME = "terraform.tfplan"
PREVIEW_DIR = "_tfworker/previews"
PENDING_DIR = "_tfworker/pending"
RECORD_DIR = "_tfworker/plans"
CONFLICT_CODES = ("PreconditionFailed", "ConditionalRequestConflict", "412", "409")


def manifest_key(plan_key: str) -> str:
    return f"{plan_key}{MANIFEST_SUFFIX}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan_base(planfile: Path) -> Tuple[Optional[str], Optional[int]]:
    """The lineage and serial of the state a saved plan was made against."""
    try:
        with ZipFile(str(planfile), "r") as archive:
            with archive.open("tfstate") as handle:
                state = json.loads(handle.read())
        return state.get("lineage") or None, state.get("serial")
    except (OSError, KeyError, ValueError, BadZipFile) as e:
        log.debug(f"unable to read the base state of {planfile}: {e}")
        return None, None


def plan_changes(planfile: Path) -> Optional[ChangeSummary]:
    """Summarize the `terraform show -json` file written beside a plan."""
    jsonfile = Path(planfile).with_suffix(".tfplan.json")
    try:
        return ChangeSummary.from_plan_json(json.loads(jsonfile.read_text()))
    except (OSError, ValueError) as e:
        log.debug(f"unable to read plan changes from {jsonfile}: {e}")
        return None


def publish_manifest(manifest: PlanManifest) -> None:
    """Announce a manifest that was just written."""
    changed_by = manifest.history[-1].run_id if manifest.history else None
    events.publish(
        PLAN_EVENT,
        manifest.deployment,
        manifest.definition,
        manifest,
        changed_by or manifest.run_id,
    )


def new_manifest(
    *,
    run_id: str,
    deployment: str,
    definition: str,
    planfile: Path,
    plan_key: str,
    storage: str,
    now: datetime,
    ttl_seconds: Optional[float],
    destroy: bool = False,
    **fields,
) -> PlanManifest:
    """Describe a plan that was just saved; it starts out pending."""
    lineage, serial = plan_base(planfile)
    return PlanManifest(
        plan_id=new_ulid(),
        run_id=run_id,
        deployment=deployment,
        definition=definition,
        destroy=destroy,
        storage=storage,
        plan_key=plan_key,
        base_lineage=lineage,
        base_serial=serial,
        sha256=sha256_file(planfile),
        size=Path(planfile).stat().st_size,
        changes=plan_changes(planfile),
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
        **fields,
    )


def refusal(manifest: PlanManifest, planfile: Path, now: datetime) -> Optional[str]:
    """
    Why a plan may not be applied, or None if it may: it must be pending,
    unexpired, and the file must be the one the manifest describes.
    """
    if manifest.status != "pending":
        return f"the plan is {manifest.status}, not pending"
    if manifest.is_expired(now):
        return f"the plan expired at {manifest.expires_at.isoformat()}"
    if sha256_file(planfile) != manifest.sha256:
        return "the plan file does not match the sha256 in its manifest"
    return None


class ManifestStore(ABC):
    @abstractmethod
    def read(self, key: str) -> Optional[Tuple[Optional[PlanManifest], str]]:
        """Return the manifest at `key` and its version tag, or None if there is none.

        An unreadable manifest is returned as (None, tag).
        """

    @abstractmethod
    def write(
        self,
        key: str,
        manifest: PlanManifest,
        if_match: Optional[str] = None,
        create_only: bool = False,
    ) -> None:
        """Write a manifest; raise ReportConflictError when a condition fails."""

    def keys_for(self, definition: Optional[str], prune: bool = True) -> Iterable[str]:
        """The manifest keys of a definition's pending backend plans, or of all
        pending backend plans when `definition` is None."""
        return []

    def retire(self, key: str, manifest: PlanManifest) -> None:
        """Remove what is left of a plan that can no longer be applied."""

    @staticmethod
    def _parse(key: str, text: str) -> Optional[PlanManifest]:
        try:
            return PlanManifest.model_validate_json(text)
        except ValueError as e:
            log.warn(f"ignoring unreadable plan manifest {key}: {e}")
            return None


class S3ManifestStore(ManifestStore):
    """
    Manifests beside plans in the state bucket.

    A pending backend plan also gets a small index entry under
    `<prefix>/_tfworker/pending/<definition>/`, so finding a definition's
    pending plans lists a few keys rather than the whole prefix.
    """

    def __init__(self, client, bucket: str, prefix: str):
        self._client = client
        self._bucket = bucket
        self._prefix = prefix

    def record_key(self, definition: str, plan_id: str) -> str:
        return f"{self._prefix}/{RECORD_DIR}/{definition}/{plan_id}.json"

    def current(self, plan_key: str) -> Optional[str]:
        """The manifest key the slot at `plan_key` points to, or None."""
        pointer = manifest_key(plan_key)
        try:
            obj = self._client.get_object(Bucket=self._bucket, Key=pointer)
            with closing(obj["Body"]) as body:
                return body.read().decode().strip()
        except botocore.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise BackendError(f"unable to read plan pointer {pointer}: {e}") from e
        except botocore.exceptions.BotoCoreError as e:
            raise BackendError(f"unable to read plan pointer {pointer}: {e}") from e

    def point(self, plan_key: str, key: str) -> None:
        pointer = manifest_key(plan_key)
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=pointer,
                Body=key.encode(),
                ContentType="text/plain",
            )
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ) as e:
            raise BackendError(f"unable to write plan pointer {pointer}: {e}") from e

    def retire(self, key, manifest):
        """
        Delete a retired plan's file and log, unless the slot already holds a
        newer plan.
        """
        try:
            if self.current(manifest.plan_key) not in (None, key):
                return
            for obj in (
                manifest.plan_key,
                manifest.plan_key.replace(".tfplan", ".log"),
            ):
                self._client.delete_object(Bucket=self._bucket, Key=obj)
        except (
            BackendError,
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ) as e:
            log.warn(f"unable to remove retired plan {manifest.plan_key}: {e}")

    def _index_key(self, manifest: PlanManifest) -> str:
        return f"{self._prefix}/{PENDING_DIR}/{manifest.definition}/{manifest.plan_id}"

    def _update_index(self, key: str, manifest: PlanManifest) -> None:
        index = self._index_key(manifest)
        try:
            if manifest.status == "pending" and manifest.storage == "backend":
                self._client.put_object(
                    Bucket=self._bucket, Key=index, Body=key.encode()
                )
            else:
                self._client.delete_object(Bucket=self._bucket, Key=index)
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ) as e:
            log.warn(f"unable to update the pending plan index {index}: {e}")

    def read(self, key):
        try:
            obj = self._client.get_object(Bucket=self._bucket, Key=key)
            with closing(obj["Body"]) as body:
                text = body.read().decode()
        except botocore.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise BackendError(f"unable to read plan manifest {key}: {e}") from e
        except botocore.exceptions.BotoCoreError as e:
            raise BackendError(f"unable to read plan manifest {key}: {e}") from e
        return self._parse(key, text), obj["ETag"]

    def write(self, key, manifest, if_match=None, create_only=False):
        kwargs = {}
        if if_match is not None:
            kwargs["IfMatch"] = if_match
        if create_only:
            kwargs["IfNoneMatch"] = "*"
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=manifest.model_dump_json(by_alias=True).encode(),
                ContentType="application/json",
                **kwargs,
            )
        except botocore.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") in CONFLICT_CODES:
                raise ReportConflictError(f"plan manifest {key} changed") from e
            raise BackendError(f"unable to write plan manifest {key}: {e}") from e
        except botocore.exceptions.BotoCoreError as e:
            raise BackendError(f"unable to write plan manifest {key}: {e}") from e
        self._update_index(key, manifest)
        publish_manifest(manifest)

    def keys_for(self, definition, prune=True):
        """
        Entries whose manifest is gone, no longer pending, or now describes
        another plan are pruned as they are found. An entry that can't be read
        is skipped, so one bad entry never hides the others. With `prune` off,
        nothing is deleted.
        """
        index = f"{self._prefix}/{PENDING_DIR}/"
        if definition is not None:
            index += f"{definition}/"
        keys = []
        entries = [key for key, _ in self._list(index, "pending plans")]
        for entry in entries:
            try:
                key = self._entry_key(entry, prune)
            except (
                BackendError,
                botocore.exceptions.ClientError,
                botocore.exceptions.BotoCoreError,
            ) as e:
                log.warn(f"skipping pending plan index entry {entry}: {e}")
                continue
            if key is not None and key not in keys:
                keys.append(key)
        return keys

    def records(self, definition: Optional[str] = None) -> List[Tuple[str, datetime]]:
        """The keys of every plan record, with when each was last written."""
        prefix = f"{self._prefix}/{RECORD_DIR}/"
        if definition is not None:
            prefix += f"{definition}/"
        return [
            (key, modified)
            for key, modified in self._list(prefix, "plan records")
            if key.endswith(".json")
        ]

    def _list(self, prefix: str, what: str) -> List[Tuple[str, datetime]]:
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            return [
                (item["Key"], item["LastModified"])
                for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix)
                for item in page.get("Contents", [])
            ]
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ) as e:
            raise BackendError(f"unable to list {what}: {e}") from e

    def _entry_key(self, entry: str, prune: bool = True) -> Optional[str]:
        """The manifest an index entry names, pruning the entry if it is stale."""
        try:
            obj = self._client.get_object(Bucket=self._bucket, Key=entry)
        except botocore.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        with closing(obj["Body"]) as body:
            key = body.read().decode()
        found = self.read(key)
        manifest = found[0] if found else None
        if (
            manifest is None
            or manifest.status != "pending"
            or manifest.plan_id != entry.rsplit("/", 1)[-1]
        ):
            if prune:
                self._client.delete_object(Bucket=self._bucket, Key=entry)
            return None
        return key


class LocalManifestStore(ManifestStore):
    """Manifests beside local plan files; the tag is the content's sha256."""

    def read(self, key):
        path = Path(key)
        try:
            text = path.read_text()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise BackendError(f"unable to read plan manifest {key}: {e}") from e
        return self._parse(key, text), hashlib.sha256(text.encode()).hexdigest()

    def write(self, key, manifest, if_match=None, create_only=False):
        """Compare and replace under an exclusive lock, so two runs can't both win."""
        path = Path(key)
        lock = path.with_name(f".{path.name}.lock")
        try:
            handle = open(lock, "a")
        except OSError as e:
            raise BackendError(f"unable to write plan manifest {key}: {e}") from e
        with handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            current = self.read(key)
            if create_only and current is not None:
                raise ReportConflictError(f"plan manifest {key} already exists")
            if if_match is not None and (current is None or current[1] != if_match):
                raise ReportConflictError(f"plan manifest {key} changed")
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            try:
                tmp.write_text(manifest.model_dump_json(by_alias=True))
                os.replace(tmp, path)
            except OSError as e:
                raise BackendError(f"unable to write plan manifest {key}: {e}") from e
        publish_manifest(manifest)


def move(
    store: ManifestStore,
    key: str,
    status: str,
    now: datetime,
    run_id: Optional[str] = None,
    reason: Optional[str] = None,
    expect: Optional[Set[str]] = None,
    plan_id: Optional[str] = None,
    retries: int = 3,
) -> Optional[PlanManifest]:
    """
    Move the manifest at `key` to `status` with a conditional write.

    Returns the updated manifest, or None when there is no manifest, it is in
    a status that cannot make the move (or is not one of `expect`), it now
    describes a plan other than `plan_id`, or other writers kept winning.
    """
    for _ in range(retries):
        found = store.read(key)
        if found is None or found[0] is None:
            return None
        current, tag = found
        if plan_id is not None and current.plan_id != plan_id:
            return None
        if expect is not None and current.status not in expect:
            return None
        if not current.can_move(status):
            return None
        updated = current.moved(status, at=now, run_id=run_id, reason=reason)
        try:
            store.write(key, updated, if_match=tag)
            return updated
        except ReportConflictError:
            log.debug(f"plan manifest {key} changed while moving to {status}; retrying")
    log.warn(f"gave up moving plan manifest {key} to {status}")
    return None


def mark_pending_stale(
    store: ManifestStore,
    definition: str,
    now: datetime,
    run_id: Optional[str],
    reason: str,
    keep: Optional[str] = None,
) -> int:
    """Mark every other pending plan for a definition stale; return how many moved."""
    moved = 0
    try:
        keys = list(store.keys_for(definition))
    except BackendError as e:
        log.warn(f"unable to find saved plans for {definition} to mark stale: {e}")
        return 0
    for key in keys:
        if key == keep:
            continue
        try:
            stale = move(store, key, "stale", now, run_id, reason, expect={"pending"})
            if stale:
                log.info(f"saved plan {stale.plan_key} is now stale: {reason}")
                store.retire(key, stale)
                moved += 1
        except BackendError as e:
            log.warn(f"unable to mark saved plan {key} stale: {e}")
    return moved
