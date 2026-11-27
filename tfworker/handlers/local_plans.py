"""
Manifests for plans saved to ``--plan-file-path``.

A plan written to a local plan file gets the same ``tfworker.plan/v1``
manifest as a plan saved in the backend, beside the file as
``<definition>.tfplan.manifest.json``. A later run that applies the file
checks and takes it the same way, so it links back to the run that made it.

Local plans are never pending fleet wide: only the holder of the file can
apply them. This handler is ready for every run that doesn't save plans in
the backend, so that any successful apply or destroy, with or without a
local plan file, marks the definition's pending backend plans stale.
"""

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Tuple

import click

import tfworker.util.log as log
from tfworker.backends import Backends
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.exceptions import BackendError, PlanRefusedError
from tfworker.report import BackendLocation
from tfworker.report.plans import PlanManifest
from tfworker.util.duration import parse_duration
from tfworker.util.plan_manifest import (
    LocalManifestStore,
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
    from tfworker.commands.terraform import TerraformResult  # noqa: F401
    from tfworker.definitions.model import Definition


@HandlerRegistry.register("local_plans", always=True)
class LocalPlanHandler(BaseHandler):
    actions = [TerraformAction.PLAN, TerraformAction.APPLY, TerraformAction.DESTROY]
    config_model = BaseConfig
    default_priority = {
        TerraformAction.PLAN: 10,
        TerraformAction.APPLY: 10,
        TerraformAction.DESTROY: 10,
    }

    def __init__(self, _: BaseConfig = None):
        self._app_state = None
        self._store = LocalManifestStore()
        self._provenance = None
        self._taken: Dict[str, Tuple[str, PlanManifest]] = {}
        self._lock = threading.Lock()
        self.execution_functions = {
            TerraformAction.PLAN: {TerraformStage.POST: self._post_plan},
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
    def app_state(self):
        if self._app_state is None:
            self._app_state = click.get_current_context().obj
        return self._app_state

    @property
    def run_id(self) -> str:
        return current_run_id(self.app_state)[0]

    def is_ready(self) -> bool:
        """Ready unless plans are saved in the backend, which the s3 handler owns."""
        return not getattr(self.app_state.root_options, "backend_plans", False)

    def _local(self) -> bool:
        """Whether this run keeps its plans on disk, where they get manifests."""
        return bool(getattr(self.app_state.terraform_options, "plan_file_path", None))

    def _preview(self) -> bool:
        return getattr(self.app_state.terraform_options, "intent", None) in (
            "preview",
            "drift",
        )

    def execute(
        self,
        action: TerraformAction,
        stage: TerraformStage,
        deployment: str,
        definition: "Definition",
        working_dir: str,
        result: Optional["TerraformResult"] = None,
    ) -> None:
        handler = self.execution_functions.get(action, {}).get(stage)
        if handler is not None:
            handler(definition, result)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _move(self, key: str, status: str, manifest=None, **kwargs):
        try:
            return move(
                self._store,
                key,
                status,
                self._now(),
                run_id=self.run_id,
                plan_id=manifest.plan_id if manifest else None,
                **kwargs,
            )
        except BackendError as e:
            log.warn(f"unable to mark plan manifest {key} {status}: {e}")
            return None

    def _post_plan(self, definition: "Definition", result) -> None:
        if not self._local():
            return
        planfile = Path(definition.plan_file)
        key = manifest_key(str(planfile))
        self._move(
            key,
            "superseded",
            expect={"pending"},
            reason=f"replaced by a plan from run {self.run_id}",
        )
        if result is None or result.exit_code != 2 or not planfile.exists():
            return
        options = self.app_state.terraform_options
        if self._provenance is None:
            self._provenance = run_provenance(self.app_state)
        manifest = new_manifest(
            run_id=self.run_id,
            deployment=self.app_state.deployment,
            definition=definition.name,
            planfile=planfile,
            plan_key=str(planfile),
            storage="preview" if self._preview() else "local",
            now=self._now(),
            ttl_seconds=parse_duration(getattr(options, "plan_ttl", None) or 0),
            destroy=bool(getattr(options, "destroy", False))
            or bool(getattr(options, "plan_destroy", False)),
            state=self._location(definition.name),
            provenance=self._provenance,
        )
        try:
            self._store.write(key, manifest)
            log.debug(f"plan manifest written: {key}")
        except BackendError as e:
            log.warn(f"unable to write plan manifest {key}: {e}")

    def _location(self, name: str):
        try:
            location = self.app_state.backend.location(name)
        except Exception as e:
            log.debug(f"unable to describe the state location of {name}: {e}")
            return None
        return location if isinstance(location, BackendLocation) else None

    def _pre_destroy(self, definition: "Definition", result) -> None:
        self._pre_apply(definition, result, destroy=True)

    def _pre_apply(self, definition: "Definition", result, destroy=False) -> None:
        if not self._local():
            return
        planfile = Path(definition.plan_file)
        key = manifest_key(str(planfile))
        try:
            found = self._store.read(key)
        except BackendError as e:
            raise PlanRefusedError(f"plan file {planfile}: {e}")
        if found is None:
            if getattr(
                self.app_state.terraform_options, "require_plan_manifest", False
            ):
                raise PlanRefusedError(
                    f"plan file {planfile} has no manifest and require_plan_manifest is set"
                )
            log.warn(
                f"plan file {planfile} has no manifest; it is applied without the apply gate"
            )
            return
        manifest = found[0]
        if manifest is None:
            raise PlanRefusedError(f"plan file {planfile} has an unreadable manifest")
        if manifest.storage == "preview" and manifest.run_id != self.run_id:
            raise PlanRefusedError(
                f"plan file {planfile}: preview plans are never applied"
            )
        if manifest.destroy != destroy:
            kind = "a destroy plan" if manifest.destroy else "not a destroy plan"
            raise PlanRefusedError(
                f"plan file {planfile}: it is {kind}, and this run is "
                f"{'destroying' if destroy else 'applying'}"
            )
        reason = refusal(manifest, planfile, self._now())
        if reason is None:
            try:
                taken = move(
                    self._store,
                    key,
                    "applying",
                    self._now(),
                    run_id=self.run_id,
                    expect={"pending"},
                    plan_id=manifest.plan_id,
                )
            except BackendError as e:
                raise PlanRefusedError(f"plan file {planfile}: unable to take it: {e}")
            if taken is None:
                reason = "it is no longer pending; another run may have taken it"
        elif manifest.status == "pending" and manifest.is_expired(self._now()):
            self._move(key, "expired", manifest, expect={"pending"}, reason=reason)
        if reason:
            raise PlanRefusedError(f"plan file {planfile}: {reason}")
        with self._lock:
            self._taken[definition.name] = (key, taken)

    def _post_apply(self, definition: "Definition", result) -> None:
        self._finish(definition.name, "applied")
        store = self._backend_store()
        if store is not None:
            mark_pending_stale(
                store,
                definition.name,
                self._now(),
                self.run_id,
                f"the state was changed by run {self.run_id}",
            )

    def _apply_failed(self, definition: "Definition", result) -> None:
        if result is not None and getattr(result, "outside_terraform", False) is True:
            self._finish(
                definition.name, "pending", reason="released: terraform did not start"
            )
        else:
            self._finish(definition.name, "failed", reason="terraform did not finish")

    def _finish(self, name: str, status: str, reason: Optional[str] = None) -> None:
        with self._lock:
            taken = self._taken.pop(name, None)
        if taken is None:
            return
        key, manifest = taken
        self._move(key, status, manifest, expect={"applying"}, reason=reason)

    def _backend_store(self) -> Optional[S3ManifestStore]:
        """The store holding the definition's backend plans, if they live in S3."""
        root = self.app_state.root_options
        if root.backend != Backends.S3:
            return None
        auth = self.app_state.authenticators.get("aws")
        if auth is None:
            return None
        try:
            client = auth.backend_session.client("s3")
        except Exception as e:
            log.warn(f"unable to reach the backend to mark saved plans stale: {e}")
            return None
        return S3ManifestStore(client, root.backend_bucket, root.backend_prefix)

    def teardown(self, deployment: str, working_dir: str) -> None:
        for name in list(self._taken):
            self._finish(
                name, "failed", reason="the run ended before the apply finished"
            )
