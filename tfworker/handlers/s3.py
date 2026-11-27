import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Union
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
    manifest_key,
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


@HandlerRegistry.register("s3", always=True)
class S3Handler(BaseHandler):
    """
    Save plans in the state bucket so a later run can apply them.

    Each saved plan has a `tfworker.plan/v1` manifest beside it. A plan is
    applied only while its manifest is pending and unexpired, its file matches
    the manifest's sha256, and it was made against the current state; the run
    that applies it first takes it with a conditional write.
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
        # the manifest of the plan each definition will apply, if it has one
        self._plans: Dict[str, Optional[PlanManifest]] = {}
        # plans this run has taken and not yet finished applying
        self._taken: Dict[str, PlanManifest] = {}
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
                TerraformStage.PRE: self._pre_apply,
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
        return getattr(self.app_state.terraform_options, "intent", None) == "preview"

    def _require_manifest(self) -> bool:
        return bool(
            getattr(self.app_state.terraform_options, "require_plan_manifest", False)
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _read_manifest(self, remotefile: str):
        try:
            return self.store.read(manifest_key(remotefile))
        except BackendError as e:
            raise HandlerError(f"Error reading plan manifest: {e}")

    def _move(self, remotefile: str, status: str, manifest=None, **kwargs):
        """Move a manifest, logging rather than failing when the store errors."""
        try:
            return move(
                self.store,
                manifest_key(remotefile),
                status,
                self._now(),
                run_id=self.run_id,
                plan_id=manifest.plan_id if manifest else None,
                **kwargs,
            )
        except BackendError as e:
            log.warn(f"unable to mark saved plan {remotefile} {status}: {e}")
            return None

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

        found = self._read_manifest(remotefile)
        if found is None:
            if self._require_manifest():
                log.warn(
                    f"saved plan {remotefile} has no manifest and require_plan_manifest is set; ignoring it"
                )
                return False
            return True
        manifest = found[0]
        if manifest is None:
            return False
        if manifest.status != "pending":
            log.debug(f"saved plan {remotefile} is {manifest.status}; ignoring it")
            return False
        if manifest.is_expired(self._now()):
            log.info(f"saved plan {remotefile} has expired")
            self._move(remotefile, "expired", manifest, expect={"pending"})
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

        if self._force_plan():
            # A forced plan supersedes the saved one, so drop it now rather than
            # relying on the upload: _post_plan only uploads when terraform
            # reports changes, so a forced plan that finds none would otherwise
            # leave the superseded plan in place for a later apply to run.
            if planfile.exists():
                planfile.unlink()
            if self._move(
                remotefile,
                "superseded",
                expect={"pending"},
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

        found = self._read_manifest(remotefile)
        manifest = found[0] if found else None
        if found is None:
            if self._require_manifest():
                log.warn(
                    f"saved plan {remotefile} has no manifest and require_plan_manifest is set; not applying it"
                )
                return False
            log.warn(
                f"saved plan {remotefile} has no manifest; it was saved by an older "
                "worker and is used without the apply gate"
            )
        elif manifest is None:
            log.warn(
                f"saved plan {remotefile} has an unreadable manifest; not using it"
            )
            return False

        if not self._s3_get_plan(planfile.resolve(), remotefile):
            return False
        if not planfile.exists():
            raise HandlerError(f"planfile not found after download: {planfile}")

        if manifest is not None:
            reason = refusal(manifest, planfile, self._now())
            if reason:
                log.warn(f"saved plan {remotefile} refused: {reason}")
                planfile.unlink()
                if manifest.status == "pending":
                    status = (
                        "expired" if manifest.is_expired(self._now()) else "discarded"
                    )
                    self._move(
                        remotefile, status, manifest, expect={"pending"}, reason=reason
                    )
                return False

        # verify the lineage and serial from the planfile matches the statefile
        if not self._verify_lineage(planfile, statefile):
            log.warn(
                f"planfile {remotefile} lineage does not match statefile, remote plan is unsuitable and will be removed"
            )
            if manifest is not None:
                self._move(
                    remotefile,
                    "stale",
                    manifest,
                    expect={"pending"},
                    reason="the state changed after the plan was made",
                )
            self._s3_delete_plan(remotefile)
            planfile.unlink()
            return False

        with self._lock:
            self._plans[definition.name] = manifest
        log.info(
            f"remote planfile downloaded: s3://{self.bucket}/{remotefile} -> {planfile}"
        )
        return True

    def _post_plan(self, definition: str, result: "TerraformResult", **kwargs):
        """
        post_plan runs after the plan is completed, it should upload the planfile to the backend
        """
        planfile = Path(definition.plan_file)
        logfile = planfile.with_suffix(".log")
        remotefile = self.get_remote_file(definition.name)
        remotelog = remotefile.replace(".tfplan", ".log")

        result.log_file(logfile.resolve())
        try:
            if planfile.exists() and result.exit_code == 2:
                if self._s3_put_plan(planfile, remotefile):
                    log.info(
                        f"remote planfile uploaded: {planfile} -> s3://{self.bucket}/{remotefile}"
                    )
                    self._write_manifest(definition, planfile, remotefile)
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
        """Describe the plan just uploaded, superseding a pending one it replaced."""
        name = definition.name
        self._move(
            remotefile,
            "superseded",
            expect={"pending"},
            reason=f"replaced by a plan from run {self.run_id}",
        )
        options = self.app_state.terraform_options
        ttl = parse_duration(getattr(options, "plan_ttl", None) or 0)
        if self._provenance is None:
            self._provenance = run_provenance(self.app_state)
        manifest = new_manifest(
            run_id=self.run_id,
            deployment=self.app_state.deployment,
            definition=name,
            planfile=planfile,
            plan_key=remotefile,
            storage="preview" if self._preview() else "backend",
            now=self._now(),
            ttl_seconds=ttl,
            destroy=bool(getattr(options, "destroy", False)),
            state=self._location(name),
            provenance=self._provenance,
            base_state_version_id=self._state_version_id(name),
        )
        try:
            self.store.write(manifest_key(remotefile), manifest)
        except BackendError as e:
            raise HandlerError(f"Error writing plan manifest: {e}")
        with self._lock:
            self._plans[name] = manifest
        log.debug(f"plan manifest written for {remotefile}: {manifest.plan_id}")

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

    def _pre_apply(self, definition: "Definition", **kwargs):
        """
        Take the saved plan before it is applied, then remove it from the
        backend so no other run can apply it.
        """
        remotefile = self.get_remote_file(definition.name)
        remotelog = remotefile.replace(".tfplan", ".log")
        with self._lock:
            manifest = self._plans.pop(definition.name, None)
        if manifest is not None:
            reason = refusal(manifest, Path(definition.plan_file), self._now())
            if reason is None:
                taken = self._move(remotefile, "applying", manifest, expect={"pending"})
                if taken is None:
                    reason = "it is no longer pending; another run may have taken it"
            if reason:
                raise PlanRefusedError(f"saved plan {remotefile}: {reason}")
            with self._lock:
                self._taken[definition.name] = taken
            log.debug(f"took saved plan {remotefile} ({taken.plan_id})")
        if self._s3_delete_plan(remotefile):
            log.debug(f"remote planfile removed: s3://{self.bucket}/{remotefile}")
        if self._s3_delete_plan(remotelog):
            log.debug(f"remote logfile removed: s3://{self.bucket}/{remotelog}")
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

    def _apply_failed(self, definition: "Definition", **kwargs):
        self._finish(definition.name, "failed", reason="terraform did not finish")

    def _finish(self, name: str, status: str, reason: Optional[str] = None) -> None:
        with self._lock:
            taken = self._taken.pop(name, None)
        if taken is None:
            return
        if self._move(
            taken.plan_key, status, taken, expect={"applying"}, reason=reason
        ):
            log.debug(f"saved plan {taken.plan_key} is now {status}")

    def teardown(self, deployment: str, working_dir: str) -> None:
        """Mark any plan this run took but never finished applying as failed."""
        for name in list(self._taken):
            self._finish(
                name, "failed", reason="the run ended before the apply finished"
            )

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
