# Copyright 2020 Richard Maynard (richard.maynard@gmail.com)
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

import boto3
import botocore
import click

from .base import BackendError, BaseBackend, validate_backend_empty


class S3Backend(BaseBackend):
    tag = "s3"
    auth_tag = "aws"

    def __init__(self, authenticators, definitions, deployment=None):
        self._authenticator = authenticators[self.auth_tag]
        self._definitions = definitions
        self._deployment = "undefined"
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

        # Initialize s3 client and create bucket if necessary. Op should create if
        # not exists
        self._s3_client = self._authenticator.backend_session.client("s3")
        try:
            self._s3_client.create_bucket(
                Bucket=self._authenticator.bucket,
                CreateBucketConfiguration={
                    "LocationConstraint": self._authenticator.region
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

    def _check_table_exists(self, name: str) -> bool:
        """ check if a supplied dynamodb table exists """
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
