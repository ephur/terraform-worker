import json
import os
from contextlib import closing
from typing import TYPE_CHECKING, Generator, Optional, Tuple, Union
from urllib.parse import quote

import boto3.dynamodb
import botocore
import botocore.errorfactory
import botocore.exceptions
import botocore.paginate
import click

import tfworker.util.log as log
import tfworker.util.terraform as tf_util
from tfworker.custom_types import JSONType
from tfworker.exceptions import BackendError, ReportConflictError
from tfworker.report import BackendLocation

from .base import BaseBackend, validate_backend_empty
from .lock import LockInfo, WorkerLease

LEASE_SUFFIX = ".tfworker-lease"
REPORT_DIR = "_tfworker"

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
    lock_inspection = True
    report_storage = True
    plan_storage = True
    tag = "s3"

    def __init__(
        self,
        authenticators: "AuthenticatorsCollection",
        deployment: str = None,
        provision: bool = True,
    ):
        """With `provision` off, a missing lock table or bucket is not created."""
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

        try:
            if provision:
                self._ensure_locking_table()
                self._ensure_backend_bucket()
            self._bucket_files: list = self._list_bucket_definitions()
        except (
            botocore.exceptions.TokenRetrievalError,
            botocore.exceptions.NoCredentialsError,
            botocore.exceptions.PartialCredentialsError,
        ) as e:
            raise BackendError(f"AWS authentication failed: {e}") from e
        except botocore.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in (
                "ExpiredToken",
                "InvalidClientTokenId",
                "SignatureDoesNotMatch",
                "AccessDenied",
            ):
                raise BackendError(f"AWS authentication failed: {e}") from e
            raise

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
        if self._uses_lockfile():
            state_config.append("    use_lockfile = true")
        else:
            state_config.append(f'    dynamodb_table = "terraform-{self._deployment}"')
        state_config.append('    encrypt = "true"')
        state_config.append("  }\n")
        return "\n".join(state_config)

    def _uses_lockfile(self) -> bool:
        """True when terraform locks state with a `.tflock` object rather than DynamoDB."""
        return tf_util.version_at_least(
            self._app_state.terraform_version
            or self._app_state.terraform_options.terraform_bin,
            1,
            12,
        )

    def _state_key(self, definition: str) -> str:
        rendered_prefix = self._app_state.root_options.backend_prefix.format(
            deployment=self._deployment
        )
        return f"{rendered_prefix}/{definition}/terraform.tfstate"

    def _get_object_text(self, key: str) -> Optional[str]:
        """Return an object's body from the backend bucket, or None if it does not exist."""
        try:
            obj = self._s3_client.get_object(
                Bucket=self._app_state.root_options.backend_bucket, Key=key
            )
            with closing(obj["Body"]) as body:
                return body.read().decode()
        except botocore.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise BackendError(f"unable to read s3 object {key}: {e}") from e
        except botocore.exceptions.BotoCoreError as e:
            raise BackendError(f"unable to read s3 object {key}: {e}") from e

    def get_lock(self, definition: str) -> Optional[LockInfo]:
        state_key = self._state_key(definition)
        if self._uses_lockfile():
            text = self._get_object_text(f"{state_key}.tflock")
            source = f"s3 lock file {state_key}.tflock"
        else:
            bucket = self._app_state.root_options.backend_bucket
            source = f"dynamodb lock {bucket}/{state_key}"
            try:
                item = self._ddb_client.get_item(
                    TableName=f"terraform-{self._deployment}",
                    Key={"LockID": {"S": f"{bucket}/{state_key}"}},
                    ConsistentRead=True,
                ).get("Item")
            except (
                botocore.exceptions.ClientError,
                botocore.exceptions.BotoCoreError,
            ) as e:
                raise BackendError(f"unable to read {source}: {e}") from e
            text = (item or {}).get("Info", {}).get("S")
        if not text:
            return None
        try:
            return LockInfo.model_validate_json(text)
        except ValueError as e:
            raise BackendError(f"unable to parse {source}: {e}") from e

    @property
    def _rendered_prefix(self) -> str:
        return self._app_state.root_options.backend_prefix.format(
            deployment=self._deployment
        )

    def _report_key(self, path: str) -> str:
        return f"{self._rendered_prefix}/{REPORT_DIR}/{path}"

    def _account_id(self) -> Optional[str]:
        if not hasattr(self, "_cached_account_id"):
            try:
                identity = self._authenticator.backend_session.client(
                    "sts"
                ).get_caller_identity()
                self._cached_account_id = identity.get("Account")
            except (
                botocore.exceptions.ClientError,
                botocore.exceptions.BotoCoreError,
            ) as e:
                log.debug(f"unable to look up the backend account id: {e}")
                self._cached_account_id = None
        return self._cached_account_id

    def location(self, definition: str) -> BackendLocation:
        return BackendLocation(
            backend=self.tag,
            account_id=self._account_id(),
            region=self._app_state.root_options.backend_region,
            bucket=self._app_state.root_options.backend_bucket,
            state_key=self._state_key(definition),
        )

    def state_version(self, definition: str) -> Optional[Tuple[str, int]]:
        text = self._get_object_text(self._state_key(definition))
        if not text:
            return None
        try:
            state = json.loads(text)
            return str(state["lineage"]), int(state["serial"])
        except (ValueError, KeyError, TypeError) as e:
            raise BackendError(
                f"unable to read the lineage of {definition} state: {e}"
            ) from e

    def read_report(self, path: str) -> Optional[Tuple[str, str]]:
        key = self._report_key(path)
        try:
            obj = self._s3_client.get_object(
                Bucket=self._app_state.root_options.backend_bucket, Key=key
            )
            with closing(obj["Body"]) as body:
                return body.read().decode(), obj["ETag"]
        except botocore.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise BackendError(f"unable to read report {key}: {e}") from e
        except botocore.exceptions.BotoCoreError as e:
            raise BackendError(f"unable to read report {key}: {e}") from e

    def write_report(
        self,
        path: str,
        body: str,
        if_match: Optional[str] = None,
        create_only: bool = False,
    ) -> None:
        key = self._report_key(path)
        kwargs = {}
        if if_match is not None:
            kwargs["IfMatch"] = if_match
        if create_only:
            kwargs["IfNoneMatch"] = "*"
        try:
            self._s3_client.put_object(
                Bucket=self._app_state.root_options.backend_bucket,
                Key=key,
                Body=body.encode(),
                ContentType="application/json",
                **kwargs,
            )
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in (
                "PreconditionFailed",
                "ConditionalRequestConflict",
                "412",
                "409",
            ):
                raise ReportConflictError(f"report {key} changed while writing") from e
            raise BackendError(f"unable to write report {key}: {e}") from e
        except botocore.exceptions.BotoCoreError as e:
            raise BackendError(f"unable to write report {key}: {e}") from e

    def _lease_key(self, definition: str, host: str) -> str:
        return f"{self._state_key(definition)}{LEASE_SUFFIX}.{quote(host, safe='')}"

    def get_lease(self, definition: str, host: str) -> Optional[WorkerLease]:
        key = self._lease_key(definition, host)
        text = self._get_object_text(key)
        if not text:
            return None
        try:
            return WorkerLease.model_validate_json(text)
        except ValueError as e:
            log.warn(f"ignoring unreadable worker lease {key}: {e}")
            return None

    def put_lease(self, lease: WorkerLease) -> None:
        key = self._lease_key(lease.definition, lease.host)
        try:
            self._s3_client.put_object(
                Bucket=self._app_state.root_options.backend_bucket,
                Key=key,
                Body=lease.model_dump_json().encode(),
                ContentType="application/json",
            )
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ) as e:
            raise BackendError(f"unable to write worker lease {key}: {e}") from e

    def delete_lease(self, definition: str, host: str) -> None:
        key = self._lease_key(definition, host)
        try:
            self._s3_client.delete_object(
                Bucket=self._app_state.root_options.backend_bucket, Key=key
            )
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ) as e:
            raise BackendError(f"unable to delete worker lease {key}: {e}") from e

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
        for page in paginator.paginate(
            Bucket=bucket_name, Prefix=prefix, StartAfter=start_after
        ):
            for content in page.get("Contents", ()):
                yield content["Key"]

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

        rendered_prefix = self._app_state.root_options.backend_prefix.format(
            deployment=self._deployment
        )
        if definition is None:
            prefix = rendered_prefix
        else:
            prefix = f"{rendered_prefix}/{definition}"

        bucket = self._app_state.root_options.backend_bucket

        report_prefix = f"{rendered_prefix}/{REPORT_DIR}/"
        for s3_object in self.filter_keys(s3_paginator, bucket, prefix):
            if s3_object.startswith(report_prefix):
                # run history is kept when states are cleaned
                continue
            if LEASE_SUFFIX in s3_object.rsplit("/", 1)[-1]:
                # a lease left by a worker that was killed; not a state
                self._delete_with_versions(s3_object)
                log.info(f"worker lease removed: {s3_object}")
                continue
            backend_file = self._s3_client.get_object(Bucket=bucket, Key=s3_object)
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
                    )  # pragma: no cover
                click.get_current_context().exit(1)
            elif "BucketAlreadyOwnedByYou" in err_str:
                log.error(f"Bucket {name} already owned by you: {err}")
                self._ctx.exit(1)
            else:
                log.error(f"Unknown error creating bucket: {err}")
                self._ctx.exit(1)

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
        bucket = self._app_state.root_options.backend_bucket
        self._s3_client.delete_object(Bucket=bucket, Key=key)

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

        self._create_bucket(bucket)
        self._create_bucket_versioning(bucket)
        self._create_bucket_public_access_block(bucket)

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
        Return the names of the definitions that have a state file in the
        deployment's prefix, see _definition_from_state_key.
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
            for key in page.get("Contents", []):
                name = _definition_from_state_key(key["Key"], prefix)
                if name is not None:
                    bucket_files.add(name)
        log.trace(f"bucket files: {bucket_files}")

        return bucket_files

    def get_state(self, remote: str) -> JSONType:
        """
        Retrieve the state item from the backend

        Args:
            remote (str): The remote name
            item (str): The item name

        Returns:
            JSONType: A JSON representation of the state item
        """

        # check if remote is a valid remote
        if remote not in self.remotes:
            raise BackendError(f"remote {remote} not found in backend")

        # get the terraform state file from S3
        state_key = (
            f"{self._app_state.root_options.backend_prefix}/{remote}/terraform.tfstate"
        )
        log.debug(f"getting state file: {state_key}")
        state_obj = self._s3_client.get_object(
            Bucket=self._app_state.root_options.backend_bucket, Key=state_key
        )

        # load the state file as JSON
        with closing(state_obj["Body"]) as body:
            state = json.load(body)

        return state


def _definition_from_state_key(key: str, prefix: str) -> Union[str, None]:
    """
    Return the definition a state key belongs to, or None for any other key.

    Only `<prefix>/<definition>/terraform.tfstate` names a definition. Saved
    plans, logs and the worker's own `_tfworker/` metadata share the prefix
    and are ignored, as is anything whose name starts with an underscore.
    """
    base = prefix.rstrip("/") + "/"
    if not key.startswith(base):
        return None
    parts = key[len(base) :].split("/")
    if len(parts) != 2 or parts[1] != "terraform.tfstate":
        return None
    if not parts[0] or parts[0].startswith("_"):
        return None
    return parts[0]
