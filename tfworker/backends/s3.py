import json
import os
from contextlib import closing
from typing import TYPE_CHECKING, Generator

import boto3.dynamodb
import botocore
import botocore.errorfactory
import botocore.paginate
import click

import tfworker.util.log as log
from tfworker.exceptions import BackendError

from .base import BaseBackend, validate_backend_empty

if TYPE_CHECKING:
    import boto3  # pragma: no cover  # noqa

    from tfworker.app_state import AppState  # pragma: no cover  # noqa
    from tfworker.authenticators import (  # pragma: no cover  # noqa
        AuthenticatorsCollection,
        AWSAuthenticator,
    )


class S3Backend(BaseBackend):
    """
    Defines how to interact with the S3 backend

    Attributes:
        auth_tag (str): The tag for the authenticator to use
        plan_storage (bool): A flag to indicate whether the backend supports plan storage
        remotes (list): A list of remote data sources based on the deployment
        tag (str): A unique identifier for the backend
        _authenticator (Authenticator): The authenticator for the backend
        _ctx (Context): The current click context
        _app_state (AppState): The current application state
        _s3_client (botocore.client.S3): The boto3 S3 client
        _deployment (str): The deployment name or "undefined"
        _ddb_client (botocore.client.DynamoDB): The boto3 DynamoDB client
        _bucket_files (set): A set of the keys in the bucket that correspond to all the definitions in a deployment
    """

    auth_tag = "aws"
    plan_storage = True
    tag = "s3"

    def __init__(
        self, authenticators: "AuthenticatorsCollection", deployment: str = None
    ):
        self._authenticator: "AWSAuthenticator" = authenticators[self.auth_tag]
        self._ctx: click.Context = click.get_current_context()
        self._app_state: "AppState" = self._ctx.obj

        if not self._authenticator.session:
            raise BackendError(
                "AWS session not available",
                help="Either provide AWS credentials or a profile, see --help for more information.",
            )
        if not self._authenticator.backend_session:
            raise BackendError(
                "AWS backend session not available",
                help="Either provide AWS credentials or a profile, see --help for more information.",
            )

        if deployment is None:
            self._deployment = "undefined"
            return

        self._deployment = deployment
        self._ddb_client: botocore.client.DynamodDB = (
            self._authenticator.backend_session.client("dynamodb")
        )
        self._s3_client: botocore.client.S3 = (
            self._authenticator.backend_session.client("s3")
        )
        log.error(f"Backend Region: {self._authenticator.backend_region}")
        self._ensure_locking_table()
        self._ensure_backend_bucket()
        self._bucket_files: list = self._list_bucket_definitions()

    @property
    def remotes(self) -> list:
        return list(self._bucket_files)

    def clean(self, deployment: str, limit: tuple = None) -> None:
        """
        clean handles determining the desired items to clean and acts as a director to the
        internal methods which handle actual resource removal

        Args:
            deployment (str): The deployment name
            limit (tuple): A tuple with a list of resources to limit execution to

        Raises:
            BackendError: An error occurred while cleaning the backend
        """
        if limit:
            for limit_item in limit:
                log.warn(
                    "when using limit, dynamodb tables won't be completely dropped"
                )
                try:
                    self._clean_bucket_state(definition=limit_item)
                    self._clean_locking_state(deployment, definition=limit_item)
                except BackendError as e:
                    raise BackendError(f"error deleting state: {e}")
        else:
            try:
                self._clean_bucket_state()
            except BackendError as e:
                raise BackendError(f"error deleting state: {e}")
            self._clean_locking_state(deployment)

    def data_hcl(self, remotes: list) -> str:
        """
        data_hcl returns the terraform configuration for the remote data sources

        Args:
            remotes (list): A list of remote sources to provide a configuration for

        Returns:
            str: The HCL configuration for the remote data source
        """

        rendered_prefix = self._app_state.root_options.backend_prefix.format(
            deployment=self._app_state.deployment
        )
        remote_data_config = []
        if type(remotes) is not list:
            raise ValueError("remotes must be a list")

        for remote in set(remotes):
            remote_data_config.append(f'data "terraform_remote_state" "{remote}" {{')
            remote_data_config.append('  backend = "s3"')
            remote_data_config.append("  config = {")
            remote_data_config.append(
                f'    region = "{self._app_state.root_options.backend_region}"'
            )
            remote_data_config.append(
                f'    bucket = "{self._app_state.root_options.backend_bucket}"'
            )
            remote_data_config.append(
                "    key =" f' "{rendered_prefix}/{remote}/terraform.tfstate"'
            )
            remote_data_config.append("  }")
            remote_data_config.append("}\n")
        return "\n".join(remote_data_config)

    def hcl(self, deployment: str) -> str:
        """
        hcl returns the configuration that belongs inside the "terraform" configuration block

        Args:
            deployment (str): The deployment name

        Returns:
            str: The HCL configuration
        """
        rendered_prefix = self._app_state.root_options.backend_prefix.format(
            deployment=self._app_state.deployment
        )
        state_config = []
        state_config.append('  backend "s3" {')
        state_config.append(
            f'    region = "{self._app_state.root_options.backend_region}"'
        )
        state_config.append(
            f'    bucket = "{self._app_state.root_options.backend_bucket}"'
        )
        state_config.append(
            f'    key = "{rendered_prefix}/{deployment}/terraform.tfstate"'
        )
        state_config.append(f'    dynamodb_table = "terraform-{self._deployment}"')
        state_config.append('    encrypt = "true"')
        state_config.append("  }\n")
        return "\n".join(state_config)

    @staticmethod
    def filter_keys(
        paginator: botocore.paginate.Paginator,
        bucket_name: str,
        prefix: str = "/",
        delimiter: str = "/",
        start_after: str = "",
    ) -> Generator[str, None, None]:
        """
        Filters the keys in a bucket based on the prefix

        adapted from: https://stackoverflow.com/questions/30249069/listing-contents-of-a-bucket-with-boto3

        Args:
            paginator (botocore.paginate.Paginator): The paginator object
            bucket_name (str): The name of the bucket
            prefix (str): The prefix to filter by
            delimiter (str): The delimiter to use
            start_after (str): The key to start after

        Yields:
            str: Any object key in the bucket contents for all pages
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

    def _check_bucket_exists(self, name: str) -> bool:
        """
        check if a supplied bucket exists

        Args:
            name (str): The name of the bucket

        Returns:
            bool: True if the bucket exists, False otherwise
        """
        try:
            self._s3_client.head_bucket(Bucket=name)
            return True
        except botocore.exceptions.ClientError as err:
            err_str = str(err)
            if "Not Found" in err_str:
                return False
            log.error(f"Error checking for bucket: {err}")
            click.get_current_context().exit(1)

    def _check_table_exists(self, name: str) -> bool:
        """
        check if a supplied dynamodb table exists

        Args:
            name (str): The name of the table

        Returns:
            bool: True if the table exists, False otherwise
        """
        try:
            log.trace(f"checking for table: {name}")
            if name in self._ddb_client.list_tables()["TableNames"]:
                return True
        except botocore.exceptions.ClientError as err:
            log.error(f"Error checking for table: {err}")
            click.get_current_context().exit(1)
        return False

    def _clean_bucket_state(self, definition: str = None) -> None:
        """
        clean_state validates all of the terraform states are empty,
        and then removes the backend objects from S3

        Args:
            definition (str): The definition

        Raises:
            BackendError: An error occurred while cleaning the state
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
                log.info(f"backend file removed: {s3_object}")
            else:
                raise BackendError(f"state file at: {s3_object} is not empty")

    def _clean_locking_state(self, deployment: str, definition: str = None) -> None:
        """
        Remove the table, or items from the locking table

        Args:
            deployment (str): The deployment name
            definition (str): The definition, if provided, only an item will be removed
        """
        bucket = self._ctx.obj.root_options.backend_bucket
        prefix = self._ctx.obj.root_options.backend_prefix.format(deployment=deployment)

        dynamo_client = self._authenticator.backend_session.resource("dynamodb")
        if definition is None:
            table = dynamo_client.Table(f"terraform-{deployment}")
            table.delete()
            log.info(f"locking table: terraform-{deployment} removed")
        else:
            # delete only the entry for a single state resource
            item = f"{bucket}/{prefix}/{definition}/terraform.tfstate-md5"
            log.info(f"removing locking table key: {item} if it exists")
            table = dynamo_client.Table(f"terraform-{deployment}")
            table.delete_item(Key={"LockID": item})

    def _create_bucket(self, name: str) -> None:
        """
        Create the S3 locking bucket

        Args:
            name (str): The name of the bucket
        """
        create_bucket_args = {
            "Bucket": name,
            "ACL": "private",
        }
        if self._authenticator.backend_session.region_name != "us-east-1":
            create_bucket_args["CreateBucketConfiguration"] = {
                "LocationConstraint": self._authenticator.backend_region
            }
        try:
            log.info(f"Creating backend bucket: {name}")
            self._s3_client.create_bucket(**create_bucket_args)
        except botocore.exceptions.ClientError as err:
            err_str = str(err)
            log.trace(f"Error creating bucket: {err}")
            if "InvalidLocationConstraint" in err_str:
                log.error(
                    "InvalidLocationConstraint raised when trying to create a bucket. "
                    "Verify the AWS backend region passed to the worker matches the "
                    "backend AWS region in the profile.",
                )
                click.get_current_context().exit(1)
            elif "BucketAlreadyExists" in err_str:
                # Ignore when testing
                if "PYTEST_CURRENT_TEST" not in os.environ:
                    log.error(
                        f"Bucket {name} already exists, this is not expected since a moment ago it did not"
                    )
                click.get_current_context().exit(1)
            elif "BucketAlreadyOwnedByYou" in err_str:
                log.error(f"Bucket {name} already owned by you: {err}")
                self._ctx.exit(1)
            else:
                raise err

    def _create_bucket_versioning(self, name: str) -> None:
        """
        Enable versioning on the bucket

        Args:
            name (str): The name of the bucket
        """
        log.info(f"Enabling versioning on bucket: {name}")
        self._s3_client.put_bucket_versioning(
            Bucket=name, VersioningConfiguration={"Status": "Enabled"}
        )

    def _create_bucket_public_access_block(self, name: str) -> None:
        """
        Block public access to the bucket

        Args:
            name (str): The name of the bucket
        """
        log.info(f"Blocking public access to bucket: {name}")
        self._s3_client.put_public_access_block(
            Bucket=name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )

    def _create_table(
        self, name: str, read_capacity: int = 1, write_capacity: int = 1
    ) -> None:
        """
        Create a dynamodb locking table.

        Args:
            name (str): The name of the table
            read_capacity (int): The read capacity units
            write_capacity (int): The write capacity units
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

    def _delete_with_versions(self, key: str) -> None:
        """
        _delete_with_versions should handle object deletions, and all references / versions of the object

        note: in initial testing this isn't required, but is inconsistent with how S3 delete markers, and the boto
        delete object call work there may be some configurations that require extra handling.
        """
        self._s3_client.delete_object(Bucket=self._authenticator.bucket, Key=key)

    def _ensure_backend_bucket(self) -> None:
        """
        _ensure_backend_bucket checks for the existence of the backend bucket, and
        creates it if it doesn't exist, along with setting the appropriate bucket
        permissions

        Raises:
            BackendError: An error occurred while ensuring the backend bucket
        """
        bucket = click.get_current_context().obj.root_options.backend_bucket
        create_bucket = self._app_state.root_options.create_backend_bucket
        bucket_present = self._check_bucket_exists(bucket)

        if bucket_present:
            log.debug(f"backend bucket {bucket} found")
            return

        if not create_bucket:
            raise BackendError(
                "Backend bucket not found and --no-create-backend-bucket specified."
            )

        self._create_bucket(self._authenticator.bucket)
        self._create_bucket_versioning(self._authenticator.bucket)
        self._create_bucket_public_access_block(self._authenticator.bucket)

    def _ensure_locking_table(self) -> None:
        """
        _ensure_locking_table checks for the existence of the locking table, and
        creates it if it doesn't exist
        """
        locking_table_name = f"terraform-{self._deployment}"
        log.debug(f"checking for locking table: {locking_table_name}")

        if self._check_table_exists(locking_table_name):
            log.debug(f"DynamoDB lock table {locking_table_name} found, continuing.")
        else:
            log.info(
                f"DynamoDB lock table {locking_table_name} not found, creating, please wait..."
            )
            self._create_table(locking_table_name)

    def _list_bucket_definitions(self) -> set:
        """
        _get_bucket_files returns a set of the keys in the bucket that correspond
        to all the definitions in a deployment, the function is poorly named.
        """
        bucket_files = set()
        root_options = click.get_current_context().obj.root_options
        bucket = root_options.backend_bucket
        prefix = root_options.backend_prefix.format(deployment=self._deployment)
        log.trace(f"listing definition prefixes in: {bucket}/{prefix}")
        s3_paginator = self._s3_client.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix
        )

        for page in s3_paginator:
            if "Contents" in page:
                for key in page["Contents"]:
                    # just append the last part of the prefix to the list, as they
                    # are relative to the base path, and deployment name
                    bucket_files.add(key["Key"].split("/")[-2])
        log.trace(f"bucket files: {bucket_files}")

        return bucket_files
