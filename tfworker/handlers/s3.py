import json
from pathlib import Path
from typing import TYPE_CHECKING, Union
from uuid import uuid4
from zipfile import ZipFile

import boto3
import botocore
import click

import tfworker.util.log as log
from tfworker.backends import Backends
from tfworker.exceptions import HandlerError
from tfworker.types.terraform import TerraformAction, TerraformStage

from .base import BaseConfig, BaseHandler
from .registry import HandlerRegistry

if TYPE_CHECKING:
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition


@HandlerRegistry.register("s3", always=True)
class S3Handler(BaseHandler):
    """The S3Handler class is a handler for the s3 backend"""

    actions = [TerraformAction.PLAN, TerraformAction.APPLY]
    config_model = BaseConfig
    _ready = False

    def __init__(self, _: BaseConfig = None):
        # defer properties until they are loaded

        self._bucket = None
        self._prefix = None
        self._s3_client = None
        self._app_state = None

        self.execution_functions = {
            TerraformAction.PLAN: {
                TerraformStage.PRE: self._check_plan,
                TerraformStage.POST: self._post_plan,
            },
            TerraformAction.APPLY: {
                TerraformStage.PRE: self._pre_apply,
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

    def get_remote_file(self, name: str) -> str:
        """get_remote_file returns the remote file path for a given name"""
        return f"{self.prefix}/{name}/terraform.tfplan"

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

    def _check_plan(self, deployment: str, definition: "Definition", **kwargs):
        """check_plan runs while the plan is being checked, it should fetch a file from the backend and store it in the local location"""
        # ensure planfile does not exist or is zero bytes if it does
        remotefile = self.get_remote_file(definition.name)
        statefile = f"{self.prefix}/{definition.name}/terraform.tfstate"
        planfile = Path(definition.plan_file)
        if planfile.exists():
            if planfile.stat().st_size == 0:
                planfile.unlink()
            else:
                raise HandlerError(f"planfile already exists: {planfile}")

        if self._s3_get_plan(planfile.resolve(), remotefile):
            if not planfile.exists():
                raise HandlerError(f"planfile not found after download: {planfile}")
            # verify the lineage and serial from the planfile matches the statefile
            if not self._verify_lineage(planfile, statefile):
                log.warn(
                    f"planfile {remotefile} lineage does not match statefile, remote plan is unsuitable and will be removed"
                )
                self._s3_delete_plan(remotefile)
                planfile.unlink()
            else:
                log.info(
                    f"remote planfile downloaded: s3://{self.bucket}/{remotefile} -> {planfile}"
                )
        return None

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
                if self._s3_put_plan(logfile, remotelog):
                    log.debug(
                        f"remote logfile uploaded: {logfile} -> s3://{self.bucket}/{remotelog}"
                    )
            return None
        except Exception as e:
            raise HandlerError(f"Error uploading planfile: {e}")
        finally:
            logfile.unlink()

    def _pre_apply(self, definition: "Definition", **kwargs):
        """_pre_apply runs before the apply is started, it should remove the planfile from the backend"""
        remotefile = self.get_remote_file(definition.name)
        remotelog = remotefile.replace(".tfplan", ".log")
        if self._s3_delete_plan(remotefile):
            log.debug(f"remote planfile removed: s3://{self.bucket}/{remotefile}")
        if self._s3_delete_plan(remotelog):
            log.debug(f"remote logfile removed: s3://{self.bucket}/{remotelog}")
        return None

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
            raise HandlerError(f"Error downloading statefile: {e}")

        # load the planfile as a json object
        plan = None
        try:
            with ZipFile(str(planfile), "r") as zip:
                with zip.open("tfstate") as f:
                    plan = json.loads(f.read())
        except Exception as e:
            raise HandlerError(f"Error loading planfile: {e}")

        # compare the lineage and serial from the planfile to the statefile
        if not (state and plan):
            return False
        if state["serial"] != plan["serial"]:
            return False
        if state["lineage"] != plan["lineage"]:
            return False

        return True
