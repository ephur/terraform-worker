import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Tuple, Union
from uuid import uuid4
from zipfile import ZipFile

import boto3
import botocore
import click

import tfworker.util.log as log
from tfworker.backends import Backends
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.exceptions import BackendError, HandlerError, PlanRefusedError
from tfworker.report import BackendLocation
from tfworker.report.plans import PlanManifest
from tfworker.util.duration import parse_duration
from tfworker.util.plan_manifest import (
    PREVIEW_DIR,
    S3ManifestStore,
    mark_pending_stale,
    move,
    new_manifest,
    refusal,
)
from tfworker.util.provenance import current_run_id, run_provenance

from .base import BaseConfig, BaseHandler
from .registry import HandlerRegistry

if TYPE_CHECKING:
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition

# intents whose plans are kept apart and never applied by a later run
UNAPPLIED_INTENTS = ("preview", "drift")
FINISH_ATTEMPTS = 3


class _Slot:
    """What a plan slot holds: a pointer to a manifest, or nothing (an older worker's plan)."""

    def __init__(self, key: Optional[str], manifest: Optional[PlanManifest]):
        self.key = key
        self.manifest = manifest

    @property
    def legacy(self) -> bool:
        return self.key is None

    @property
    def unreadable(self) -> bool:
        return self.key is not None and self.manifest is None


@HandlerRegistry.register("s3", always=True)
class S3Handler(BaseHandler):
    """
    Save plans in the state bucket so a later run can apply them.

    Each saved plan has a `tfworker.plan/v1` manifest. A plan is applied only
    while its manifest is pending and unexpired, its file matches the
    manifest's sha256, it is the kind of plan (apply or destroy) the run is
    for, and it was made against the current state; the run that applies it
    first takes it with a conditional write.
    """

    actions = [TerraformAction.PLAN, TerraformAction.APPLY, TerraformAction.DESTROY]
    config_model = BaseConfig
    _ready = False
    default_priority = {
        TerraformAction.PLAN: 10,
        TerraformAction.APPLY: 10,
        TerraformAction.DESTROY: 10,
    }

    def __init__(self, _: BaseConfig = None):
        # defer properties until they are loaded

        self._bucket = None
        self._prefix = None
        self._s3_client = None
        self._app_state = None
        self._store = None
        self._provenance = None
        # the slot of the plan each definition will apply
        self._plans: Dict[str, _Slot] = {}
        # plans this run has taken and not yet finished applying
        self._taken: Dict[str, Tuple[str, PlanManifest]] = {}
        # S3 version of each definition's state when its plan started
        self._base_versions: Dict[str, Optional[str]] = {}
        self._lock = threading.Lock()

        self.execution_functions = {
            TerraformAction.PLAN: {
                TerraformStage.PRE: self._check_plan,
                TerraformStage.POST: self._post_plan,
            },
            TerraformAction.APPLY: {
                TerraformStage.PRE: self._pre_apply,
                TerraformStage.POST: self._post_apply,
                TerraformStage.ERROR: self._apply_failed,
            },
            TerraformAction.DESTROY: {
                TerraformStage.PRE: self._pre_destroy,
                TerraformStage.POST: self._post_apply,
                TerraformStage.ERROR: self._apply_failed,
            },
        }

    @property
    def bucket(self):
        if self._bucket is None:
            self._bucket = self.app_state.root_options.backend_bucket
        return self._bucket

    @property
    def prefix(self):
        if self._prefix is None:
            self._prefix = self.app_state.root_options.backend_prefix
        return self._prefix

    @property
    def s3_client(self):
        if self._s3_client is None:
            self._s3_client = self.app_state.authenticators[
                "aws"
            ].backend_session.client("s3")
        return self._s3_client

    @property
    def app_state(self):
        if self._app_state is None:
            self._app_state = click.get_current_context().obj
        return self._app_state

    @property
    def store(self) -> S3ManifestStore:
        if self._store is None:
            self._store = S3ManifestStore(self.s3_client, self.bucket, self.prefix)
        return self._store

    @property
    def run_id(self) -> str:
        return current_run_id(self.app_state)[0]

    def _preview(self) -> bool:
        return (
            getattr(self.app_state.terraform_options, "intent", None)
            in UNAPPLIED_INTENTS
        )

    def _require_manifest(self) -> bool:
        return bool(
            getattr(self.app_state.terraform_options, "require_plan_manifest", False)
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _slot(self, remotefile: str) -> _Slot:
        """Read the manifest the slot at `remotefile` points to."""
        try:
            key = self.store.current(remotefile)
            if key is None:
                return _Slot(None, None)
            found = self.store.read(key)
        except BackendError as e:
            raise HandlerError(f"Error reading plan manifest: {e}")
        return _Slot(key, found[0] if found else None)

    def _move(self, key: str, status: str, manifest=None, **kwargs):
        """Move a manifest, logging rather than failing when the store errors."""
        try:
            return move(
                self.store,
                key,
                status,
                self._now(),
                run_id=self.run_id,
                plan_id=manifest.plan_id if manifest else None,
                **kwargs,
            )
        except BackendError as e:
            log.warn(f"unable to mark saved plan {key} {status}: {e}")
            return None

    def _retire(self, slot: _Slot, status: str, reason: Optional[str] = None):
        """Move a pending plan to a terminal status and remove its file."""
        moved = self._move(
            slot.key, status, slot.manifest, expect={"pending"}, reason=reason
        )
        if moved is not None:
            self.store.retire(slot.key, moved)
        return moved

    def is_ready(self) -> bool:
        """
        is_ready performs a test to ensure that the handler is able to perform
        the required operations in s3
        """
        if self.app_state.root_options.backend != Backends.S3:
            return False
        if self.app_state.root_options.backend_plans is not True:
            return False
        if self.app_state.authenticators.get("aws") is None:
            return False

        if self._ready is not True:
            filename = str(uuid4().hex[:6].upper())
            if self.s3_client.list_objects(
                Bucket=self.bucket,
                Prefix=f"{self.prefix}/{filename}",
            ).get("Contents"):
                raise HandlerError(
                    f"Error initializing S3Handler, remote file already exists: {filename}"
                )
            try:
                self.s3_client.upload_file(
                    "/dev/null",
                    self._bucket,
                    f"{self.prefix}/{filename}",
                )
            except boto3.exceptions.S3UploadFailedError as e:
                raise HandlerError(
                    f"Error initializing S3Handler, could not create file: {e}"
                )
            try:
                self.s3_client.delete_object(
                    Bucket=self.bucket,
                    Key=f"{self.prefix}/{filename}",
                )
            except boto3.exceptions.S3UploadFailedError as e:
                raise HandlerError(
                    f"Error initializing S3Handler, could not delete file: {e}"
                )
            self._ready = True
        return self._ready

    def has_plan(self, definition: "Definition") -> bool:
        """
        has_plan checks if a remote plan file exists for the given definition.
        This allows the system to skip init/plan if a plan is already available.

        Only a pending, unexpired plan is offered. A manifest that can't be
        read is logged and treated as no plan, never as a reason to stop.
        """
        if not self.is_ready():
            return False

        if self._preview():
            return False

        remotefile = self.get_remote_file(definition.name)

        try:
            response = self.s3_client.head_object(Bucket=self.bucket, Key=remotefile)
            if response.get("ContentLength", 0) <= 0:
                return False
        except botocore.exceptions.ClientError:
            return False

        try:
            slot = self._slot(remotefile)
        except HandlerError as e:
            log.warn(f"saved plan {remotefile} is not offered: {e}")
            return False
        if slot.legacy:
            if self._require_manifest():
                log.warn(
                    f"saved plan {remotefile} has no manifest and require_plan_manifest is set; ignoring it"
                )
                return False
            return True
        if slot.unreadable:
            return False
        if slot.manifest.status != "pending":
            log.debug(f"saved plan {remotefile} is {slot.manifest.status}; ignoring it")
            return False
        if slot.manifest.is_expired(self._now()):
            log.info(f"saved plan {remotefile} has expired")
            self._retire(slot, "expired")
            return False
        return True

    def get_remote_file(self, name: str) -> str:
        """get_remote_file returns the remote file path for a given name"""
        if self._preview():
            return f"{self.prefix}/{PREVIEW_DIR}/{self.run_id}/{name}/terraform.tfplan"
        run_id = self.app_state.root_options.run_id
        run_prefix = f"{run_id}/" if run_id else ""
        return f"{self.prefix}/{run_prefix}{name}/terraform.tfplan"

    def execute(
        self,
        action: "TerraformAction",
        stage: "TerraformStage",
        deployment: str,
        definition: "Definition",
        working_dir: str,
        result: Union["TerraformResult", None] = None,
    ) -> None:  # pragma: no cover
        # save a copy of the planfile to the backend state bucket
        if action in self.execution_functions.keys():
            if stage in self.execution_functions[action].keys():
                self.execution_functions[action][stage](
                    deployment=deployment,
                    definition=definition,
                    working_dir=working_dir,
                    result=result,
                )

    def _force_plan(self) -> bool:
        """Whether this run plans regardless of any saved plan."""
        return bool(getattr(self.app_state.terraform_options, "force_plan", False))

    def _check_plan(self, deployment: str, definition: "Definition", **kwargs):
        """check_plan runs while the plan is being checked, it should fetch a file from the backend and store it in the local location"""
        remotefile = self.get_remote_file(definition.name)
        planfile = Path(definition.plan_file)
        with self._lock:
            self._base_versions[definition.name] = self._state_version_id(
                definition.name
            )

        if self._force_plan():
            # A forced plan supersedes the saved one, so drop it now rather than
            # relying on the upload: _post_plan only uploads when terraform
            # reports changes, so a forced plan that finds none would otherwise
            # leave the superseded plan in place for a later apply to run.
            if planfile.exists():
                planfile.unlink()
            slot = self._slot(remotefile)
            if not slot.legacy and not slot.unreadable:
                if self._retire(
                    slot,
                    "superseded",
                    reason=f"replaced by a forced plan in run {self.run_id}",
                ):
                    log.info(f"forced plan: saved plan {remotefile} is now superseded")
            if self._s3_delete_plan(remotefile):
                log.debug(
                    f"forced plan: discarded saved plan s3://{self.bucket}/{remotefile}"
                )
            self._s3_delete_plan(remotefile.replace(".tfplan", ".log"))
            return None

        # ensure planfile does not exist or is zero bytes if it does
        if planfile.exists():
            if planfile.stat().st_size == 0:
                planfile.unlink()
            else:
                raise HandlerError(f"planfile already exists: {planfile}")

        if self._preview():
            return None
        self.get_plan(definition)
        return None

    def get_plan(self, definition: "Definition") -> bool:
        """
        Download the saved plan has_plan reported and leave it at the
        definition's plan file, refusing one that fails the apply gate.

        Returns:
            bool: True if a usable plan is in place for this definition
        """
        remotefile = self.get_remote_file(definition.name)
        statefile = f"{self.prefix}/{definition.name}/terraform.tfstate"
        planfile = Path(definition.plan_file)

        slot = self._slot(remotefile)
        if slot.unreadable:
            log.warn(
                f"saved plan {remotefile} has an unreadable manifest; not using it"
            )
            return False
        if slot.manifest is not None and slot.manifest.status != "pending":
            log.debug(f"saved plan {remotefile} is {slot.manifest.status}")
            return False

        if not self._s3_get_plan(planfile.resolve(), remotefile):
            return False
        if not planfile.exists():
            raise HandlerError(f"planfile not found after download: {planfile}")

        if slot.legacy:
            if self._require_manifest():
                log.warn(
                    f"saved plan {remotefile} has no manifest and require_plan_manifest is set; not applying it"
                )
                planfile.unlink()
                return False
            log.warn(
                f"saved plan {remotefile} has no manifest; it was saved by an older "
                "worker and is used without the apply gate"
            )
        else:
            reason = refusal(slot.manifest, planfile, self._now())
            if reason:
                log.warn(f"saved plan {remotefile} refused: {reason}")
                planfile.unlink()
                expired = slot.manifest.is_expired(self._now())
                self._retire(slot, "expired" if expired else "discarded", reason)
                return False

        # verify the lineage and serial from the planfile matches the statefile
        if not self._verify_lineage(planfile, statefile):
            log.warn(
                f"planfile {remotefile} lineage does not match statefile, remote plan is unsuitable and will be removed"
            )
            if not slot.legacy:
                self._retire(
                    slot, "stale", reason="the state changed after the plan was made"
                )
            self._s3_delete_plan(remotefile)
            planfile.unlink()
            return False

        with self._lock:
            self._plans[definition.name] = slot
        log.info(
            f"remote planfile downloaded: s3://{self.bucket}/{remotefile} -> {planfile}"
        )
        return True

    def _post_plan(self, definition: str, result: "TerraformResult", **kwargs):
        """
        Upload the plan to the backend. Its manifest is written first, so a
        plan object is never found without one.
        """
        planfile = Path(definition.plan_file)
        logfile = planfile.with_suffix(".log")
        remotefile = self.get_remote_file(definition.name)
        remotelog = remotefile.replace(".tfplan", ".log")

        result.log_file(logfile.resolve())
        try:
            if (
                planfile.exists()
                and planfile.stat().st_size > 0
                and result.exit_code == 2
            ):
                self._write_manifest(definition, planfile, remotefile)
                if self._s3_put_plan(planfile, remotefile):
                    log.info(
                        f"remote planfile uploaded: {planfile} -> s3://{self.bucket}/{remotefile}"
                    )
                if self._s3_put_plan(logfile, remotelog):
                    log.debug(
                        f"remote logfile uploaded: {logfile} -> s3://{self.bucket}/{remotelog}"
                    )
            return None
        except HandlerError:
            raise
        except Exception as e:
            raise HandlerError(f"Error uploading planfile: {e}")
        finally:
            logfile.unlink()

    def _write_manifest(
        self, definition: "Definition", planfile: Path, remotefile: str
    ) -> None:
        """Record the plan about to be uploaded, superseding a pending one it replaces."""
        name = definition.name
        slot = self._slot(remotefile)
        if not slot.legacy and not slot.unreadable:
            self._retire(
                slot,
                "superseded",
                reason=f"replaced by a plan from run {self.run_id}",
            )
        options = self.app_state.terraform_options
        ttl = parse_duration(getattr(options, "plan_ttl", None) or 0)
        if self._provenance is None:
            self._provenance = run_provenance(self.app_state)
        with self._lock:
            version_id = self._base_versions.get(name)
        manifest = new_manifest(
            run_id=self.run_id,
            deployment=self.app_state.deployment,
            definition=name,
            planfile=planfile,
            plan_key=remotefile,
            storage="preview" if self._preview() else "backend",
            now=self._now(),
            ttl_seconds=ttl,
            destroy=_destroy_plan(options),
            state=self._location(name),
            provenance=self._provenance,
            base_state_version_id=version_id,
        )
        key = self.store.record_key(name, manifest.plan_id)
        try:
            self.store.write(key, manifest, create_only=True)
            self.store.point(remotefile, key)
        except BackendError as e:
            raise HandlerError(f"Error writing plan manifest: {e}")
        with self._lock:
            self._plans[name] = _Slot(key, manifest)
        log.debug(f"plan manifest written for {remotefile}: {key}")

    def _location(self, name: str):
        try:
            location = self.app_state.backend.location(name)
        except Exception as e:
            log.debug(f"unable to describe the state location of {name}: {e}")
            return None
        return location if isinstance(location, BackendLocation) else None

    def _state_version_id(self, name: str) -> Optional[str]:
        try:
            head = self.s3_client.head_object(
                Bucket=self.bucket, Key=f"{self.prefix}/{name}/terraform.tfstate"
            )
        except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError):
            return None
        version = head.get("VersionId")
        return None if version in (None, "null") else version

    def _pre_destroy(self, definition: "Definition", **kwargs):
        self._pre_apply(definition, destroy=True)

    def _pre_apply(self, definition: "Definition", destroy: bool = False, **kwargs):
        """
        Take the saved plan before it is applied, refusing one that fails the
        gate. A plan already on disk is checked against the slot's manifest
        too, as when `--plan-file-path` keeps a copy of a backend plan.
        """
        name = definition.name
        remotefile = self.get_remote_file(name)
        with self._lock:
            slot = self._plans.pop(name, None)
        if slot is None:
            try:
                slot = self._slot(remotefile)
            except HandlerError as e:
                raise PlanRefusedError(f"saved plan {remotefile}: {e}")
            if slot.legacy and self._s3_exists(remotefile):
                if self._require_manifest():
                    raise PlanRefusedError(
                        f"saved plan {remotefile} has no manifest and require_plan_manifest is set"
                    )
        if slot.unreadable:
            raise PlanRefusedError(
                f"saved plan {remotefile} has an unreadable manifest"
            )
        if slot.legacy:
            # an older worker's plan: remove it so no other run applies it
            self._delete_remote_plan(remotefile)
            return None

        manifest = slot.manifest
        # a run may apply its own preview plan (always_apply); no later run may
        if manifest.storage == "preview" and manifest.run_id != self.run_id:
            raise PlanRefusedError(
                f"saved plan {remotefile}: preview plans are never applied"
            )
        if manifest.destroy != destroy:
            kind = "a destroy plan" if manifest.destroy else "not a destroy plan"
            raise PlanRefusedError(
                f"saved plan {remotefile}: it is {kind}, and this run is "
                f"{'destroying' if destroy else 'applying'}"
            )
        reason = refusal(manifest, Path(definition.plan_file), self._now())
        if reason:
            if manifest.status == "pending" and manifest.is_expired(self._now()):
                self._retire(slot, "expired", reason)
            raise PlanRefusedError(f"saved plan {remotefile}: {reason}")
        try:
            taken = move(
                self.store,
                slot.key,
                "applying",
                self._now(),
                run_id=self.run_id,
                expect={"pending"},
                plan_id=manifest.plan_id,
            )
        except BackendError as e:
            raise PlanRefusedError(f"saved plan {remotefile}: unable to take it: {e}")
        if taken is None:
            raise PlanRefusedError(
                f"saved plan {remotefile}: it is no longer pending; another run may have taken it"
            )
        with self._lock:
            self._taken[name] = (slot.key, taken)
        log.debug(f"took saved plan {remotefile} ({taken.plan_id})")
        return None

    def _post_apply(self, definition: "Definition", **kwargs):
        """Mark the plan applied and every other pending plan for the definition stale."""
        self._finish(definition.name, "applied")
        mark_pending_stale(
            self.store,
            definition.name,
            self._now(),
            self.run_id,
            f"the state was changed by run {self.run_id}",
        )

    def _apply_failed(
        self, definition: "Definition", result: "TerraformResult" = None, **kwargs
    ):
        """
        A plan whose apply never reached terraform (a refusal or a failed
        hook) is released back to pending; otherwise it failed.
        """
        if result is not None and getattr(result, "outside_terraform", False) is True:
            self._finish(
                definition.name,
                "pending",
                reason="released: terraform did not start",
                retire=False,
            )
        else:
            self._finish(definition.name, "failed", reason="terraform did not finish")

    def _finish(
        self,
        name: str,
        status: str,
        reason: Optional[str] = None,
        retire: bool = True,
    ) -> None:
        with self._lock:
            taken = self._taken.pop(name, None)
        if taken is None:
            return
        key, manifest = taken
        for attempt in range(FINISH_ATTEMPTS):
            try:
                moved = move(
                    self.store,
                    key,
                    status,
                    self._now(),
                    run_id=self.run_id,
                    reason=reason,
                    expect={"applying"},
                    plan_id=manifest.plan_id,
                )
                break
            except BackendError as e:
                log.warn(
                    f"unable to mark saved plan {manifest.plan_key} {status} "
                    f"(attempt {attempt + 1} of {FINISH_ATTEMPTS}): {e}"
                )
        else:
            return
        if moved is None:
            return
        log.debug(f"saved plan {manifest.plan_key} is now {status}")
        if retire:
            self.store.retire(key, moved)

    def teardown(self, deployment: str, working_dir: str) -> None:
        """Mark any plan this run took but never finished applying as failed."""
        for name in list(self._taken):
            self._finish(
                name, "failed", reason="the run ended before the apply finished"
            )

    def _delete_remote_plan(self, remotefile: str) -> None:
        remotelog = remotefile.replace(".tfplan", ".log")
        if self._s3_delete_plan(remotefile):
            log.debug(f"remote planfile removed: s3://{self.bucket}/{remotefile}")
        if self._s3_delete_plan(remotelog):
            log.debug(f"remote logfile removed: s3://{self.bucket}/{remotelog}")

    def _s3_exists(self, remotefile: str) -> bool:
        try:
            self.s3_client.head_object(Bucket=self.bucket, Key=remotefile)
            return True
        except botocore.exceptions.ClientError:
            return False

    def _s3_get_plan(self, planfile: Path, remotefile: str) -> bool:
        """_get_plan downloads the file from s3"""
        # fetch the planfile from the backend
        downloaded = False
        try:
            self.s3_client.download_file(self.bucket, remotefile, planfile)
            # make sure the local file exists, and is greater than 0 bytes
            downloaded = True
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "404":
                log.debug(f"remote plan {remotefile} not found")
                pass
            else:
                raise HandlerError(f"Error downloading planfile: {e}")
        return downloaded

    def _s3_put_plan(self, planfile: Path, remotefile: str) -> bool:
        """_put_plan uploads the file to s3"""
        uploaded = False
        # don't upload empty plans
        if planfile.stat().st_size == 0:
            return uploaded
        try:
            self.s3_client.upload_file(str(planfile), self.bucket, remotefile)
            uploaded = True
        except botocore.exceptions.ClientError as e:
            raise HandlerError(f"Error uploading planfile: {e}")
        return uploaded

    def _s3_delete_plan(self, remotefile: str) -> bool:
        """_delete_plan removes a remote plan file"""
        deleted = False
        try:
            self.s3_client.delete_object(Bucket=self.bucket, Key=remotefile)
            deleted = True
        except botocore.exceptions.ClientError as e:
            raise HandlerError(f"Error deleting planfile: {e}")
        return deleted

    def _verify_lineage(self, planfile: Path, statefile: str) -> bool:
        # load the statefile as a json object from the backend
        state = None
        try:
            state = json.loads(
                self.s3_client.get_object(Bucket=self.bucket, Key=statefile)[
                    "Body"
                ].read()
            )
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                raise HandlerError(f"Error downloading statefile: {e}")
            # no prior state; only a plan made against empty state is valid
            state = None

        # load the planfile as a json object
        plan = None
        try:
            with ZipFile(str(planfile), "r") as zip:
                with zip.open("tfstate") as f:
                    plan = json.loads(f.read())
        except Exception as e:
            raise HandlerError(f"Error loading planfile: {e}")

        if state is None:
            return bool(plan) and plan.get("serial") == 0 and not plan.get("lineage")

        # compare the lineage and serial from the planfile to the statefile
        if not (state and plan):
            return False
        if state["serial"] != plan["serial"]:
            return False
        if state["lineage"] != plan["lineage"]:
            return False

        return True


def _destroy_plan(options) -> bool:
    """Whether terraform made a destroy plan, as it does for either flag."""
    return bool(getattr(options, "destroy", False)) or bool(
        getattr(options, "plan_destroy", False)
    )
