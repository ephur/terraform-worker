# Copyright 2020-2023 Richard Maynard (richard.maynard@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import boto3
import botocore
import click

from ..handlers import BaseHandler, HandlerError
from .base import BackendError, BaseBackend, validate_backend_empty


class S3Backend(BaseBackend):
    tag = "s3"
    auth_tag = "aws"
    plan_storage = False

    def __init__(self, authenticators, definitions, deployment=None):
        self._authenticator = authenticators[self.auth_tag]
        self._definitions = definitions
        self._deployment = "undefined"
        self._handlers = None

        if deployment:
            self._deployment = deployment

        self._ddb_client = boto3.client(
            "dynamodb",
            region_name=self._authenticator.backend_region,
            aws_access_key_id=self._authenticator.access_key_id,
            aws_secret_access_key=self._authenticator.secret_access_key,
            aws_session_token=self._authenticator.session_token,
        )

        locking_table_name = f"terraform-{deployment}"

        # Check locking table for aws backend
        click.secho(
            f"Checking backend locking table: {locking_table_name}", fg="yellow"
        )

        if self._check_table_exists(locking_table_name):
            click.secho("DynamoDB lock table found, continuing.", fg="yellow")
        else:
            click.secho(
                "DynamoDB lock table not found, creating, please wait...", fg="yellow"
            )
            self._create_table(locking_table_name)

        # Initialize s3 client and create bucket if necessary.
        self._s3_client = self._authenticator.backend_session.client("s3")
        try:
            self._s3_client.head_bucket(Bucket=self._authenticator.bucket)
        except botocore.exceptions.ClientError as err:
            err_str = str(err)
            if "Not Found" not in err_str:
                raise err
            if self._authenticator.create_backend_bucket:
                try:
                    self._s3_client.create_bucket(
                        Bucket=self._authenticator.bucket,
                        CreateBucketConfiguration={
                            "LocationConstraint": self._authenticator.backend_region
                        },
                        ACL="private",
                    )
                except botocore.exceptions.ClientError as err:
                    err_str = str(err)
                    if "InvalidLocationConstraint" in err_str:
                        click.secho(
                            "InvalidLocationConstraint raised when trying to create a bucket. "
                            "Verify the AWS Region passed to the worker matches the AWS region "
                            "in the profile.",
                            fg="red",
                        )
                    elif "BucketAlreadyExists" in err_str:
                        # Ignore when testing
                        if "PYTEST_CURRENT_TEST" not in os.environ:
                            click.secho(err_str, fg="red")
                            sys.exit(4)
                    elif "BucketAlreadyOwnedByYou" not in err_str:
                        raise err

                # Block public access
                self._s3_client.put_public_access_block(
                    Bucket=self._authenticator.bucket,
                    PublicAccessBlockConfiguration={
                        "BlockPublicAcls": True,
                        "IgnorePublicAcls": True,
                        "BlockPublicPolicy": True,
                        "RestrictPublicBuckets": True,
                    },
                )

                # Enable versioning on the bucket
                s3_resource = self._authenticator.backend_session.resource("s3")
                versioning = s3_resource.BucketVersioning(self._authenticator.bucket)
                versioning.enable()
            else:
                raise BackendError(
                    "Backend bucket not found and --no-create-backend-bucket specified."
                )

        # Generate a list of all files in the bucket, at the desired prefix for the deployment, used for "--all-remote-states" option and clean
        s3_paginator = self._s3_client.get_paginator("list_objects_v2").paginate(
            Bucket=self._authenticator.bucket,
            Prefix=self._authenticator.prefix,
        )

        self._bucket_files = set()
        for page in s3_paginator:
            if "Contents" in page:
                for key in page["Contents"]:
                    # just append the last part of the prefix to the list
                    self._bucket_files.add(key["Key"].split("/")[-2])
        try:
            self._handlers = S3Handler(self._authenticator)
            self.plan_storage = True
        except HandlerError as e:
            click.secho(f"Error initializing S3Handler: {e}")
            raise SystemExit(1)

    @property
    def handlers(self) -> dict:
        """
        handlers returns a dictionary of handlers for the backend, ensure a singleton
        """
        return {self.tag: self._handlers}

    def remotes(self) -> list:
        """return a list of the remote bucket keys"""
        return list(self._bucket_files)

    def _check_table_exists(self, name: str) -> bool:
        """check if a supplied dynamodb table exists"""
        if name in self._ddb_client.list_tables()["TableNames"]:
            return True
        return False

    def _clean_bucket_state(self, definition=None):
        """
        clean_state validates all of the terraform states are empty,
        and then removes the backend objects from S3

        optionally definition can be passed to limit the cleanup
        to a single definition
        """
        s3_paginator = self._s3_client.get_paginator("list_objects_v2")

        if definition is None:
            prefix = self._authenticator.prefix
        else:
            prefix = f"{self._authenticator.prefix}/{definition}"

        for s3_object in self.filter_keys(
            s3_paginator, self._authenticator.bucket, prefix
        ):
            backend_file = self._s3_client.get_object(
                Bucket=self._authenticator.bucket, Key=s3_object
            )
            body = backend_file["Body"]
            with closing(backend_file["Body"]):
                backend = json.load(body)

            if validate_backend_empty(backend):
                self._delete_with_versions(s3_object)
                click.secho(f"backend file removed: {s3_object}", fg="yellow")
            else:
                raise BackendError(f"state file at: {s3_object} is not empty")

    def _clean_locking_state(self, deployment, definition=None):
        """
        clean_locking_state when called removes the dynamodb table
        that holds all of the state checksums and locking table
        entries
        """
        dynamo_client = self._authenticator.backend_session.resource("dynamodb")
        if definition is None:
            table = dynamo_client.Table(f"terraform-{deployment}")
            table.delete()
            click.secho(f"locking table: terraform-{deployment} removed", fg="yellow")
        else:
            # delete only the entry for a single state resource
            table = dynamo_client.Table(f"terraform-{deployment}")
            table.delete_item(
                Key={
                    "LockID": f"{self._authenticator.bucket}/{self._authenticator.prefix}/{definition}/terraform.tfstate-md5"
                }
            )
            click.secho(
                f"locking table key: '{self._authenticator.bucket}/{self._authenticator.prefix}/{definition}/terraform.tfstate-md5' removed",
                fg="yellow",
            )

    def _create_table(
        self, name: str, read_capacity: int = 1, write_capacity: int = 1
    ) -> None:
        """
        Create a dynamodb locking table.
        """
        table_key = "LockID"
        self._ddb_client.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": table_key, "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": table_key, "AttributeType": "S"},
            ],
            ProvisionedThroughput={
                "ReadCapacityUnits": read_capacity,
                "WriteCapacityUnits": write_capacity,
            },
        )

        self._ddb_client.get_waiter("table_exists").wait(
            TableName=name, WaiterConfig={"Delay": 10, "MaxAttempts": 30}
        )

    def _delete_with_versions(self, key):
        """
        _delete_with_versions should handle object deletions, and all references / versions of the object

        note: in initial testing this isn't required, but is inconsistent with how S3 delete markers, and the boto
        delete object call work there may be some configurations that require extra handling.
        """
        self._s3_client.delete_object(Bucket=self._authenticator.bucket, Key=key)

    def clean(self, deployment: str, limit: tuple = None) -> None:
        """
        clean handles determining the desired items to clean and acts as a director to the
        internal methods which handle actual resource removal
        """
        if limit:
            for limit_item in limit:
                click.secho(
                    "when using limit, dynamodb tables won't be completely dropped",
                    fg="yellow",
                )
                try:
                    # the bucket state deployment is part of the s3 prefix
                    self._clean_bucket_state(definition=limit_item)
                    # deployment name needs specified to determine the dynamo table
                    self._clean_locking_state(deployment, definition=limit_item)
                except BackendError as e:
                    click.secho(f"error deleting state: {e}", fg="red")
                    raise SystemExit(1)
        else:
            try:
                self._clean_bucket_state()
            except BackendError as e:
                click.secho(f"error deleting state: {e}")
                raise SystemExit(1)
            self._clean_locking_state(deployment)

    def hcl(self, name: str) -> str:
        state_config = []
        state_config.append('  backend "s3" {')
        state_config.append(f'    region = "{self._authenticator.backend_region}"')
        state_config.append(f'    bucket = "{self._authenticator.bucket}"')
        state_config.append(
            f'    key = "{self._authenticator.prefix}/{name}/terraform.tfstate"'
        )
        state_config.append(f'    dynamodb_table = "terraform-{self._deployment}"')
        state_config.append('    encrypt = "true"')
        state_config.append("  }")
        return "\n".join(state_config)

    def data_hcl(self, remotes: list) -> str:
        remote_data_config = []
        if type(remotes) is not list:
            raise ValueError("remotes must be a list")

        for remote in set(remotes):
            remote_data_config.append(f'data "terraform_remote_state" "{remote}" {{')
            remote_data_config.append('  backend = "s3"')
            remote_data_config.append("  config = {")
            remote_data_config.append(
                f'    region = "{self._authenticator.backend_region}"'
            )
            remote_data_config.append(f'    bucket = "{self._authenticator.bucket}"')
            remote_data_config.append(
                "    key ="
                f' "{self._authenticator.prefix}/{remote}/terraform.tfstate"'
            )
            remote_data_config.append("  }")
            remote_data_config.append("}\n")
        return "\n".join(remote_data_config)

    @staticmethod
    def filter_keys(paginator, bucket_name, prefix="/", delimiter="/", start_after=""):
        """
        filter_keys returns just they keys that are needed
        primarily from: https://stackoverflow.com/questions/30249069/listing-contents-of-a-bucket-with-boto3
        """

        prefix = prefix[1:] if prefix.startswith(delimiter) else prefix
        start_after = (
            (start_after or prefix) if prefix.endswith(delimiter) else start_after
        )
        try:
            for page in paginator.paginate(
                Bucket=bucket_name, Prefix=prefix, StartAfter=start_after
            ):
                for content in page.get("Contents", ()):
                    yield content["Key"]
        except TypeError:
            pass


class S3Handler(BaseHandler):
    """The S3Handler class is a handler for the s3 backend"""

    actions = ["plan", "apply"]
    required_vars = []
    _is_ready = False

    def __init__(self, authenticator):
        try:
            self.execution_functions = {
                "plan": {
                    "check": self._check_plan,
                    "post": self._post_plan,
                },
                "apply": {
                    "pre": self._pre_apply,
                },
            }

            self._authenticator = authenticator
            self._s3_client = self._authenticator.backend_session.client("s3")

        except Exception as e:
            raise HandlerError(f"Error initializing S3Handler: {e}")

    def is_ready(self):
        if not self._is_ready:
            filename = str(uuid4().hex[:6].upper())
            if self._s3_client.list_objects(
                Bucket=self._authenticator.bucket,
                Prefix=f"{self._authenticator.prefix}/{filename}",
            ).get("Contents"):
                raise HandlerError(
                    f"Error initializing S3Handler, remote file already exists: {filename}"
                )
            try:
                self._s3_client.upload_file(
                    "/dev/null",
                    self._authenticator.bucket,
                    f"{self._authenticator.prefix}/{filename}",
                )
            except boto3.exceptions.S3UploadFailedError as e:
                raise HandlerError(
                    f"Error initializing S3Handler, could not create file: {e}"
                )
            try:
                self._s3_client.delete_object(
                    Bucket=self._authenticator.bucket,
                    Key=f"{self._authenticator.prefix}/{filename}",
                )
            except boto3.exceptions.S3UploadFailedError as e:
                raise HandlerError(
                    f"Error initializing S3Handler, could not delete file: {e}"
                )
            self._is_ready = True
        return self._is_ready

    def execute(self, action, stage, **kwargs):
        # save a copy of the planfile to the backend state bucket
        if action in self.execution_functions.keys():
            if stage in self.execution_functions[action].keys():
                self.execution_functions[action][stage](**kwargs)
        return None

    def _check_plan(self, planfile: Path, definition: str, **kwargs):
        """check_plan runs while the plan is being checked, it should fetch a file from the backend and store it in the local location"""
        # ensure planfile does not exist or is zero bytes if it does
        remotefile = f"{self._authenticator.prefix}/{definition}/{planfile.name}"
        if planfile.exists():
            if planfile.stat().st_size == 0:
                planfile.unlink()
            else:
                raise HandlerError(f"planfile already exists: {planfile}")

        if self._s3_get_plan(planfile, remotefile):
            if not planfile.exists():
                raise HandlerError(f"planfile not found after download: {planfile}")
            click.secho(
                f"remote planfile downloaded: s3://{self._authenticator.bucket}/{remotefile} -> {planfile}",
                fg="yellow",
            )

    def _post_plan(
        self, planfile: Path, definition: str, changes: bool = False, **kwargs
    ):
        """post_apply runs after the apply is complete, it should upload the planfile to the backend"""
        logfile = planfile.with_suffix(".log")
        remotefile = f"{self._authenticator.prefix}/{definition}/{planfile.name}"
        remotelog = remotefile.replace(".tfplan", ".log")
        if "text" in kwargs.keys():
            with open(logfile, "w") as f:
                f.write(kwargs["text"])
        if planfile.exists() and changes:
            if self._s3_put_plan(planfile, remotefile):
                click.secho(
                    f"remote planfile uploaded: {planfile} -> s3://{self._authenticator.bucket}/{remotefile}",
                    fg="yellow",
                )
                if self._s3_put_plan(logfile, remotelog):
                    click.secho(
                        f"remote logfile uploaded: {logfile} -> s3://{self._authenticator.bucket}/{remotelog}",
                        fg="yellow",
                    )
        return None

    def _pre_apply(self, planfile: Path, definition: str, **kwargs):
        """_pre_apply runs before the apply is started, it should remove the planfile from the backend"""
        logfile = planfile.with_suffix(".log")
        remotefile = f"{self._authenticator.prefix}/{definition}/{planfile.name}"
        remotelog = remotefile.replace(".tfplan", ".log")
        if self._s3_delete_plan(remotefile, planfile):
            click.secho(
                f"remote planfile removed: s3://{self._authenticator.bucket}/{remotefile}",
                fg="yellow",
            )
        if self._s3_delete_plan(remotelog, logfile):
            click.secho(
                f"remote logfile removed: s3://{self._authenticator.bucket}/{remotelog}",
                fg="yellow",
            )
        return None

    def _s3_get_plan(self, planfile: Path, remotefile: str) -> bool:
        """_get_plan downloads the file from s3"""
        # fetch the planfile from the backend
        downloaded = False
        try:
            self._s3_client.download_file(
                self._authenticator.bucket, remotefile, planfile
            )
            # make sure the local file exists, and is greater than 0 bytes
            downloaded = True
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "404":
                click.secho(f"remote plan {remotefile} not found", fg="yellow")
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
            self._s3_client.upload_file(
                str(planfile), self._authenticator.bucket, remotefile
            )
            uploaded = True
        except botocore.exceptions.ClientError as e:
            raise HandlerError(f"Error uploading planfile: {e}")
        return uploaded

    def _s3_delete_plan(self, remotefile: str, planfile: str) -> bool:
        """_delete_plan removes a remote plan file"""
        deleted = False
        try:
            self._s3_client.delete_object(
                Bucket=self._authenticator.bucket, Key=remotefile
            )
            deleted = True
        except botocore.exceptions.ClientError as e:
            raise HandlerError(f"Error deleting planfile: {e}")
        return deleted
