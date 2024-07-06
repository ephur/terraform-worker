import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import botocore
import click
import pytest
from moto import mock_aws

import tfworker.util.log as log
from tfworker.backends.s3 import S3Backend
from tfworker.cli_options import CLIOptionsRoot
from tfworker.exceptions import BackendError

log.log_level = log.LogLevel.TRACE


@pytest.fixture
def mock_cli_options_root():
    mock_root = MagicMock(spec=CLIOptionsRoot)
    mock_root.region = "us-east-1"
    mock_root.backend_region = "us-east-1"
    mock_root.backend_bucket = "test-bucket"
    mock_root.backend_prefix = "prefix"
    mock_root.create_backend_bucket = True
    return mock_root


@pytest.fixture
def mock_cli_options_root_backend_west():
    mock_root = MagicMock(spec=CLIOptionsRoot)
    mock_root.region = "us-east-1"
    mock_root.backend_region = "us-west-2"
    mock_root.backend_bucket = "west-test-bucket"
    mock_root.backend_prefix = "prefix"
    mock_root.create_backend_bucket = True
    return mock_root


@pytest.fixture
def mock_app_state(mock_cli_options_root):
    mock_state = MagicMock()
    mock_state.root_options = mock_cli_options_root
    mock_state.deployment = "test-deployment"
    return mock_state


@pytest.fixture
def mock_app_state_backend_west(mock_cli_options_root_backend_west):
    mock_state = MagicMock()
    mock_state.root_options = mock_cli_options_root_backend_west
    mock_state.deployment = "test-deployment"
    return mock_state


@pytest.fixture
def mock_authenticators():
    os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
    os.environ["AWS_ACCESS_KEY_ID"] = "test"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

    mock_auth = MagicMock()
    mock_auth["aws"].session = boto3.Session()
    mock_auth["aws"].backend_session = mock_auth["aws"].session
    mock_auth["aws"].bucket = "test-bucket"
    mock_auth["aws"].prefix = "prefix"
    mock_auth["aws"].backend_region = "us-east-1"
    return mock_auth


@pytest.fixture
def mock_authenticators_backend_west():
    os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
    os.environ["AWS_ACCESS_KEY_ID"] = "test"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

    mock_auth = MagicMock()
    mock_auth["aws"].session = boto3.Session()
    mock_auth["aws"].backend_session = boto3.Session(region_name="us-west-2")
    mock_auth["aws"].bucket = "west-test-bucket"
    mock_auth["aws"].prefix = "prefix"
    mock_auth["aws"].backend_region = "us-west-2"
    return mock_auth


@pytest.fixture
def mock_click_context(mock_app_state):
    ctx = MagicMock(spec=click.Context)
    ctx.obj = mock_app_state
    ctx.exit = MagicMock(side_effect=sys.exit)
    return ctx


@pytest.fixture
def mock_click_context_backend_west(mock_app_state_backend_west):
    ctx = MagicMock(spec=click.Context)
    ctx.obj = mock_app_state_backend_west
    ctx.exit = MagicMock(side_effect=sys.exit)
    return ctx


@pytest.fixture(autouse=True)
def setup_method(mocker, mock_click_context):
    mocker.patch("click.get_current_context", return_value=mock_click_context)


class TestS3BackendInit:
    @mock_aws
    def test_init_success(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        assert backend._deployment == "test-deployment"
        assert backend._s3_client is not None
        assert backend._ddb_client is not None
        assert backend._bucket_files is not None

    @mock_aws
    def test_init_success_alt_region(
        self, mock_authenticators_backend_west, mocker, mock_click_context_backend_west
    ):
        mocker.patch(
            "click.get_current_context", return_value=mock_click_context_backend_west
        )
        backend = S3Backend(mock_authenticators_backend_west, "test-deployment")
        assert backend._deployment == "test-deployment"
        assert backend._s3_client is not None
        assert backend._ddb_client is not None
        assert backend._bucket_files is not None

    @mock_aws
    def test_init_success_undefined_deployment(
        self, mock_authenticators, mock_click_context
    ):
        backend = S3Backend(mock_authenticators)
        assert backend._deployment == "undefined"
        assert not hasattr(backend, "_s3_client")
        assert not hasattr(backend, "_ddb_client")
        assert not hasattr(backend, "_bucket_files")

    @mock_aws
    def test_init_no_aws_session(self, mock_authenticators):
        mock_authenticators["aws"].session = None
        with pytest.raises(BackendError, match="AWS session not available"):
            S3Backend(mock_authenticators, "test-deployment")

    @mock_aws
    def test_init_no_backend_session(self, mock_authenticators):
        mock_authenticators["aws"].backend_session = None
        with pytest.raises(BackendError, match="AWS backend session not available"):
            S3Backend(mock_authenticators, "test-deployment")

    @mock_aws
    def test_init_no_create_bucket(
        self, mock_authenticators, mocker, mock_click_context
    ):
        mock_click_context.obj.root_options.create_backend_bucket = False
        mocker.patch("click.get_current_context", return_value=mock_click_context)
        with pytest.raises(
            BackendError,
            match="Backend bucket not found and --no-create-backend-bucket specified",
        ):
            S3Backend(mock_authenticators, "test-deployment")


class TestS3BackendCheckBucketExists:

    @mock_aws
    def test_check_bucket_exists(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="test-bucket")
        assert backend._check_bucket_exists("test-bucket") is True

    @mock_aws
    def test_check_bucket_does_not_exist(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        assert backend._check_bucket_exists("non-existent-bucket") is False

    @mock_aws
    def test_check_bucket_error(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        with patch.object(
            backend._s3_client,
            "head_bucket",
            side_effect=botocore.exceptions.ClientError(
                {"Error": {"Code": "403"}}, "HeadBucket"
            ),
        ):
            with pytest.raises(SystemExit):
                backend._check_bucket_exists("test-bucket")


class TestS3BackendEnsureLockingTable:

    @mock_aws
    def setup_method(self, _):
        self.dynamodb = boto3.client("dynamodb")
        self.cleanup_tables()

    @mock_aws
    def teardown_method(self, _):
        self.cleanup_tables()

    def cleanup_tables(self):
        tables = self.dynamodb.list_tables()["TableNames"]
        for table in tables:
            self.dynamodb.delete_table(TableName=table)
            self.dynamodb.get_waiter("table_not_exists").wait(TableName=table)

    @mock_aws
    def test_ensure_locking_table_exists(self, mock_authenticators):
        self.dynamodb.create_table(
            TableName="terraform-test-deployment-exists",
            KeySchema=[{"AttributeName": "LockID", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "LockID", "AttributeType": "S"}],
            ProvisionedThroughput={"ReadCapacityUnits": 1, "WriteCapacityUnits": 1},
        )
        tables_len = len(self.dynamodb.list_tables()["TableNames"])
        backend = S3Backend(mock_authenticators, "test-deployment-exists")
        backend._ddb_client = self.dynamodb
        tables_len_after = len(self.dynamodb.list_tables()["TableNames"])
        assert tables_len == tables_len_after

    @mock_aws
    def test_ensure_locking_table_create(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment-new")
        backend._ensure_locking_table()
        tables = self.dynamodb.list_tables()["TableNames"]
        assert "terraform-test-deployment-new" in tables


class TestS3BackendCheckTableExists:

    @mock_aws
    def test_check_table_exists(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        assert backend._check_table_exists("terraform-test-deployment") is True

    @mock_aws
    def test_check_table_does_not_exist(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        assert backend._check_table_exists("non-existent-table") is False

    @mock_aws
    def test_check_table_error(self, mock_authenticators, mock_click_context):
        backend = S3Backend(mock_authenticators, "test-deployment")
        with patch.object(
            backend._ddb_client,
            "list_tables",
            side_effect=botocore.exceptions.ClientError(
                {"Error": {"Code": "ResourceNotFoundException"}}, "DescribeTable"
            ),
        ):
            with pytest.raises(SystemExit):
                backend._check_table_exists("terraform-test-deployment")
            mock_click_context.exit.assert_called_once_with(1)


class TestS3BackendListBucketFiles:

    @mock_aws
    def test_list_bucket_files(self, mock_authenticators):
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="test-bucket")
        s3.put_object(
            Bucket="test-bucket", Key="prefix/test-deployment/def1/file1", Body=b"test"
        )
        s3.put_object(
            Bucket="test-bucket", Key="prefix/test-deployment/def2/file2", Body=b"test"
        )
        backend = S3Backend(mock_authenticators, "test-deployment")
        bucket_files = backend._list_bucket_definitions()
        assert "def1" in bucket_files
        assert "def2" in bucket_files
        assert sorted(backend.remotes) == sorted(["def1", "def2"])


class TestS3BackendClean:

    @mock_aws
    def setup_method(self, _):
        self.s3 = boto3.client("s3")
        self.dynamodb = boto3.client("dynamodb")
        self.s3.create_bucket(Bucket="test-bucket")
        self.dynamodb.create_table(
            TableName="terraform-test-deployment",
            KeySchema=[{"AttributeName": "LockID", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "LockID", "AttributeType": "S"}],
            ProvisionedThroughput={"ReadCapacityUnits": 1, "WriteCapacityUnits": 1},
        )
        self.dynamodb.get_waiter("table_exists").wait(
            TableName="terraform-test-deployment"
        )

    @mock_aws
    def teardown_method(self, _):
        tables = self.dynamodb.list_tables()["TableNames"]
        for table in tables:
            self.dynamodb.delete_table(TableName=table)
            self.dynamodb.get_waiter("table_not_exists").wait(TableName=table)

        try:
            bucket_objects = self.s3.list_objects_v2(Bucket="test-bucket").get(
                "Contents", []
            )
            for obj in bucket_objects:
                self.s3.delete_object(Bucket="test-bucket", Key=obj["Key"])
            self.s3.delete_bucket(Bucket="test-bucket")
        except self.s3.exceptions.NoSuchBucket:
            pass

    @mock_aws
    def test_clean_with_limit(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        self.s3.put_object(Bucket="test-bucket", Key="prefix/resource1")
        with patch.object(
            backend, "_clean_bucket_state"
        ) as mock_clean_bucket_state, patch.object(
            backend, "_clean_locking_state"
        ) as mock_clean_locking_state:
            backend.clean("test-deployment", limit=("resource1",))
            mock_clean_bucket_state.assert_called_once_with(definition="resource1")
            mock_clean_locking_state.assert_called_once_with(
                "test-deployment", definition="resource1"
            )

    @mock_aws
    def test_clean_without_limit(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        self.s3.put_object(Bucket="test-bucket", Key="prefix/test-deployment/file1")
        with patch.object(
            backend, "_clean_bucket_state"
        ) as mock_clean_bucket_state, patch.object(
            backend, "_clean_locking_state"
        ) as mock_clean_locking_state:
            backend.clean("test-deployment")
            mock_clean_bucket_state.assert_called_once_with()
            mock_clean_locking_state.assert_called_once_with("test-deployment")

    @mock_aws
    def test_clean_raises_backend_error(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        with patch.object(
            backend,
            "_clean_bucket_state",
            side_effect=BackendError("error deleting state"),
        ):
            with pytest.raises(BackendError, match="error deleting state"):
                backend.clean("test-deployment")

    @mock_aws
    def test_clean_with_limit_clean_bucket_state_error(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        self.s3.put_object(Bucket="test-bucket", Key="prefix/resource1")
        with patch.object(
            backend,
            "_clean_bucket_state",
            side_effect=BackendError("error deleting state"),
        ):
            with pytest.raises(BackendError, match="error deleting state"):
                backend.clean("test-deployment", limit=("resource1",))

    @mock_aws
    def test_clean_with_limit_clean_locking_state_error(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        self.s3.put_object(Bucket="test-bucket", Key="prefix/resource1")
        with patch.object(backend, "_clean_bucket_state", return_value=None):
            with patch.object(
                backend,
                "_clean_locking_state",
                side_effect=BackendError("error deleting state"),
            ):
                with pytest.raises(BackendError, match="error deleting state"):
                    backend.clean("test-deployment", limit=("resource1",))


class TestS3BackendDataHcl:
    @mock_aws
    def test_data_hcl_success(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        remotes = ["remote1", "remote2"]
        result = backend.data_hcl(remotes)
        assert 'data "terraform_remote_state" "remote1"' in result
        assert 'data "terraform_remote_state" "remote2"' in result
        assert 'backend = "s3"' in result
        assert 'region = "us-east-1"' in result
        assert 'bucket = "test-bucket"' in result
        assert 'key = "prefix/remote1/terraform.tfstate"' in result
        assert 'key = "prefix/remote2/terraform.tfstate"' in result

    @mock_aws
    def test_data_hcl_invalid_remotes(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        with pytest.raises(ValueError, match="remotes must be a list"):
            backend.data_hcl("invalid_remote")


class TestS3BackendHcl:
    @mock_aws
    def test_hcl_success(self, mock_authenticators, mock_click_context):
        backend = S3Backend(mock_authenticators, "test-deployment")
        result = backend.hcl("test-deployment")
        assert 'backend "s3" {' in result
        assert 'region = "us-east-1"' in result
        assert 'bucket = "test-bucket"' in result
        assert 'key = "prefix/test-deployment/terraform.tfstate"' in result
        assert 'dynamodb_table = "terraform-test-deployment"' in result
        assert 'encrypt = "true"' in result


class TestS3BackendFilterKeys:

    @mock_aws
    def setup_s3(self):
        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.s3.create_bucket(Bucket="test-bucket")
        # Add objects to the bucket
        self.s3.put_object(Bucket="test-bucket", Key="prefix/file1", Body="data")
        self.s3.put_object(Bucket="test-bucket", Key="prefix/file2", Body="data")
        self.s3.put_object(Bucket="test-bucket", Key="prefix/dir/file3", Body="data")
        self.s3.put_object(Bucket="test-bucket", Key="other-prefix/file4", Body="data")

    @mock_aws
    def test_filter_keys_no_prefix(self):
        self.setup_s3()
        paginator = self.s3.get_paginator("list_objects_v2")
        keys = list(S3Backend.filter_keys(paginator, "test-bucket"))
        assert "prefix/file1" in keys
        assert "prefix/file2" in keys
        assert "prefix/dir/file3" in keys
        assert "other-prefix/file4" in keys

    @mock_aws
    def test_filter_keys_with_prefix(self):
        self.setup_s3()
        paginator = self.s3.get_paginator("list_objects_v2")
        keys = list(S3Backend.filter_keys(paginator, "test-bucket", prefix="prefix/"))
        assert "prefix/file1" in keys
        assert "prefix/file2" in keys
        assert "prefix/dir/file3" in keys
        assert "other-prefix/file4" not in keys

    @mock_aws
    def test_filter_keys_with_delimiter(self):
        self.setup_s3()
        paginator = self.s3.get_paginator("list_objects_v2")
        keys = list(
            S3Backend.filter_keys(
                paginator, "test-bucket", prefix="prefix/", delimiter="/"
            )
        )
        assert "prefix/file1" in keys
        assert "prefix/file2" in keys
        assert "prefix/dir/file3" in keys
        assert "other-prefix/file4" not in keys

    # @TODO: Fix the test, or code, start_after is not working as expected here, but is not used in the code
    # @mock_aws
    # def test_filter_keys_with_start_after(self):
    #     self.setup_s3()
    #     paginator = self.s3.get_paginator("list_objects_v2")
    #     keys = list(S3Backend.filter_keys(paginator, "test-bucket", prefix="prefix", start_after="prefix/file1"))
    #     log.trace(keys)
    #     assert "prefix/file1" not in keys
    #     assert "prefix/file2" in keys
    #     assert "prefix/dir/file3" in keys
    #     assert "other-prefix/file4" not in keys


class TestS3BackendCleanBucketState:
    @mock_aws
    def setup_s3(self, empty_state, occupied_state, all_empty=False):
        if all_empty:
            occupied_state = empty_state
        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.s3.create_bucket(Bucket="test-bucket")
        # Add objects to the bucket
        self.s3.put_object(
            Bucket="test-bucket", Key="prefix/def1/terraform.tfstate", Body=empty_state
        )
        self.s3.put_object(
            Bucket="test-bucket", Key="prefix/def2/terraform.tfstate", Body=empty_state
        )
        self.s3.put_object(
            Bucket="test-bucket",
            Key="prefix/def3/terraform.tfstate",
            Body=occupied_state,
        )
        self.s3.put_object(
            Bucket="test-bucket",
            Key="prefix/def4/terraform.tfstate",
            Body=occupied_state,
        )

    @mock_aws
    def test_clean_bucket_state(self, mock_authenticators, empty_state, occupied_state):
        self.setup_s3(empty_state, occupied_state, all_empty=True)
        backend = S3Backend(mock_authenticators, "test-deployment")
        backend._clean_bucket_state()
        keys = list(
            S3Backend.filter_keys(
                self.s3.get_paginator("list_objects_v2"), "test-bucket"
            )
        )
        assert len(keys) == 0

    @mock_aws
    def test_clean_bucket_state_with_definition(
        self, mock_authenticators, empty_state, occupied_state
    ):
        self.setup_s3(empty_state, occupied_state)
        backend = S3Backend(mock_authenticators, "test-deployment")
        backend._clean_bucket_state(definition="def1")
        keys = list(
            S3Backend.filter_keys(
                self.s3.get_paginator("list_objects_v2"), "test-bucket"
            )
        )
        assert len(keys) == 3
        assert "prefix/def1/terraform.tfstate" not in keys
        assert "prefix/def2/terraform.tfstate" in keys
        assert "prefix/def3/terraform.tfstate" in keys
        assert "prefix/def4/terraform.tfstate" in keys

    @mock_aws
    def test_clean_bucket_state_raises_backend_error(
        self, mock_authenticators, empty_state, occupied_state
    ):
        self.setup_s3(empty_state, occupied_state)
        backend = S3Backend(mock_authenticators, "test-deployment")
        with pytest.raises(BackendError, match="not empty"):
            backend._clean_bucket_state(definition="def3")
        keys = list(
            S3Backend.filter_keys(
                self.s3.get_paginator("list_objects_v2"), "test-bucket"
            )
        )
        assert len(keys) == 4
        assert "prefix/def1/terraform.tfstate" in keys
        assert "prefix/def2/terraform.tfstate" in keys
        assert "prefix/def3/terraform.tfstate" in keys
        assert "prefix/def4/terraform.tfstate" in keys


class TestS3BackendCleanLockingState:
    @mock_aws
    def setup_ddb(self, deployment, lock_id):
        self.dynamodb = boto3.client("dynamodb")
        self.dynamodb.create_table(
            TableName=f"terraform-{deployment}",
            KeySchema=[{"AttributeName": "LockID", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "LockID", "AttributeType": "S"}],
            ProvisionedThroughput={"ReadCapacityUnits": 1, "WriteCapacityUnits": 1},
        )
        self.dynamodb.put_item(
            TableName=f"terraform-{deployment}",
            Item={"LockID": {"S": lock_id}},
        )

    @mock_aws
    def test_clean_locking_state(self, mock_authenticators):
        self.setup_ddb(
            "test-deployment", "test-bucket/prefix/lock1/terraform.tfstate-md5"
        )
        backend = S3Backend(mock_authenticators, "test-deployment")
        backend._clean_locking_state("test-deployment")
        tables = self.dynamodb.list_tables()["TableNames"]
        assert "terraform-test-deployment" not in tables

    @mock_aws
    def test_clean_locking_state_with_definition(self, mock_authenticators):
        self.setup_ddb(
            "test-deployment", "test-bucket/prefix/lock1/terraform.tfstate-md5"
        )
        backend = S3Backend(mock_authenticators, "test-deployment")
        backend._clean_locking_state("test-deployment", definition="lock1")
        tables = self.dynamodb.list_tables()["TableNames"]
        items = self.dynamodb.scan(TableName="terraform-test-deployment")["Items"]
        log.trace(items)
        assert len(items) == 0
        assert "terraform-test-deployment" in tables

    @mock_aws
    def test_clean_locking_state_with_bad_key(self, mock_authenticators):
        self.setup_ddb(
            "test-deployment", "test-bucket/prefix/lock1/terraform.tfstate-md5"
        )
        backend = S3Backend(mock_authenticators, "test-deployment")
        backend._clean_locking_state("test-deployment", definition="lock2")
        tables = self.dynamodb.list_tables()["TableNames"]
        items = self.dynamodb.scan(TableName="terraform-test-deployment")["Items"]
        assert len(items) == 1
        assert "terraform-test-deployment" in tables


class TestS3BackendCreateBucket:
    @mock_aws
    def test_create_bucket_success(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        backend._create_bucket("test-bucket")
        s3 = boto3.client("s3")
        response = s3.list_buckets()
        buckets = [bucket["Name"] for bucket in response["Buckets"]]
        assert "test-bucket" in buckets

    @mock_aws
    def test_create_bucket_invalid_location_constraint(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        with patch.object(
            backend._s3_client,
            "create_bucket",
            side_effect=botocore.exceptions.ClientError(
                {"Error": {"Code": "InvalidLocationConstraint"}}, "CreateBucket"
            ),
        ):
            with pytest.raises(SystemExit):
                backend._create_bucket("test-bucket")

    @mock_aws
    def test_create_bucket_already_exists(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        with patch.object(
            backend._s3_client,
            "create_bucket",
            side_effect=botocore.exceptions.ClientError(
                {"Error": {"Code": "BucketAlreadyExists"}}, "CreateBucket"
            ),
        ):
            with pytest.raises(SystemExit):
                backend._create_bucket("test-bucket")

    @mock_aws
    def test_create_bucket_already_exists_alt_region(
        self, mock_authenticators_backend_west, mocker, mock_click_context_backend_west
    ):
        mocker.patch(
            "click.get_current_context", return_value=mock_click_context_backend_west
        )
        backend = S3Backend(mock_authenticators_backend_west, "test-deployment")
        s3 = boto3.client("s3", region_name="us-west-2")
        s3.create_bucket(
            Bucket="already-exists-test-bucket",
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        with pytest.raises(SystemExit):
            backend._create_bucket("already-exists-test-bucket")
