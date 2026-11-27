from unittest.mock import patch

import boto3
import botocore
import pytest
from moto import mock_aws

import tfworker.util.log as log
from tfworker.backends.s3 import S3Backend, _definition_from_state_key
from tfworker.exceptions import BackendError


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

    @pytest.mark.parametrize(
        "error_factory,error_args",
        [
            (
                botocore.exceptions.TokenRetrievalError,
                {"provider": "sso", "error_msg": "Token has expired"},
            ),
            (botocore.exceptions.NoCredentialsError, {}),
            (
                botocore.exceptions.PartialCredentialsError,
                {"provider": "test", "cred_var": "AWS_SECRET_ACCESS_KEY"},
            ),
        ],
        ids=["token_retrieval", "no_credentials", "partial_credentials"],
    )
    @mock_aws
    def test_init_auth_errors(
        self, mock_authenticators, mocker, error_factory, error_args
    ):
        """Test that various auth errors raise helpful BackendError."""
        error = error_factory(**error_args)
        mocker.patch.object(S3Backend, "_check_table_exists", side_effect=error)

        with pytest.raises(
            BackendError, match="AWS authentication failed:"
        ) as exc_info:
            S3Backend(mock_authenticators, "test-deployment")

        assert exc_info.value.__cause__ is error

    @pytest.mark.parametrize(
        "error_code",
        [
            "ExpiredToken",
            "InvalidClientTokenId",
            "SignatureDoesNotMatch",
            "AccessDenied",
        ],
        ids=["expired_token", "invalid_token", "bad_signature", "access_denied"],
    )
    @mock_aws
    def test_init_client_error_auth_codes(
        self, mock_authenticators, mocker, error_code
    ):
        """Test that ClientError with auth codes raises helpful BackendError."""
        client_error = botocore.exceptions.ClientError(
            {"Error": {"Code": error_code, "Message": "Auth error"}},
            "DescribeTable",
        )
        mocker.patch.object(S3Backend, "_check_table_exists", side_effect=client_error)

        with pytest.raises(
            BackendError, match="AWS authentication failed:"
        ) as exc_info:
            S3Backend(mock_authenticators, "test-deployment")

        assert exc_info.value.__cause__ is client_error

    @mock_aws
    def test_init_non_auth_client_error(self, mock_authenticators, mocker):
        """Test that non-auth ClientError is re-raised."""
        client_error = botocore.exceptions.ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "DescribeTable",
        )
        mocker.patch.object(
            S3Backend,
            "_check_table_exists",
            side_effect=client_error,
        )

        # Should re-raise the original ClientError, not BackendError
        with pytest.raises(botocore.exceptions.ClientError):
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
        for key in (
            "prefix/def1/terraform.tfstate",
            "prefix/def2/terraform.tfstate",
            # none of these name a definition
            "prefix/def1/terraform.tfplan",
            "prefix/run-1/def3/terraform.tfplan",
            "prefix/_tfworker/runs/2026/11/27/run.json",
            "prefix/_reserved/terraform.tfstate",
            "prefix-other/def4/terraform.tfstate",
        ):
            s3.put_object(Bucket="test-bucket", Key=key, Body=b"test")
        backend = S3Backend(mock_authenticators, "test-deployment")
        bucket_files = backend._list_bucket_definitions()
        assert bucket_files == {"def1", "def2"}
        assert sorted(backend.remotes) == sorted(["def1", "def2"])


class TestDefinitionFromStateKey:
    @pytest.mark.parametrize(
        "key, expected",
        [
            ("p/dep/def1/terraform.tfstate", "def1"),
            ("p/dep/def1/terraform.tfplan", None),
            ("p/dep/def1/terraform.tfstate.tflock", None),
            ("p/dep/run/def1/terraform.tfstate", None),
            ("p/dep/terraform.tfstate", None),
            ("p/dep//terraform.tfstate", None),
            ("p/dep/_tfworker/terraform.tfstate", None),
            ("p/dep2/def1/terraform.tfstate", None),
            ("other/dep/def1/terraform.tfstate", None),
        ],
    )
    def test_definition_from_state_key(self, key, expected):
        assert _definition_from_state_key(key, "p/dep") == expected

    def test_trailing_slash_on_prefix(self):
        assert _definition_from_state_key("p/dep/d/terraform.tfstate", "p/dep/") == "d"


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
        with (
            patch.object(backend, "_clean_bucket_state") as mock_clean_bucket_state,
            patch.object(backend, "_clean_locking_state") as mock_clean_locking_state,
        ):
            backend.clean("test-deployment", limit=("resource1",))
            mock_clean_bucket_state.assert_called_once_with(definition="resource1")
            mock_clean_locking_state.assert_called_once_with(
                "test-deployment", definition="resource1"
            )

    @mock_aws
    def test_clean_without_limit(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        self.s3.put_object(Bucket="test-bucket", Key="prefix/test-deployment/file1")
        with (
            patch.object(backend, "_clean_bucket_state") as mock_clean_bucket_state,
            patch.object(backend, "_clean_locking_state") as mock_clean_locking_state,
        ):
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
    def test_hcl_success_dynamodb(
        self, mock_authenticators, mock_click_context, mocker
    ):
        mocker.patch("tfworker.util.terraform.version_at_least", return_value=False)
        backend = S3Backend(mock_authenticators, "test-deployment")
        result = backend.hcl("test-deployment")
        assert 'backend "s3" {' in result
        assert 'region = "us-east-1"' in result
        assert 'bucket = "test-bucket"' in result
        assert 'key = "prefix/test-deployment/terraform.tfstate"' in result
        assert 'dynamodb_table = "terraform-test-deployment"' in result
        assert 'encrypt = "true"' in result

    @mock_aws
    def test_hcl_success_lockfile(
        self, mock_authenticators, mock_click_context, mocker
    ):
        mocker.patch("tfworker.util.terraform.version_at_least", return_value=True)
        backend = S3Backend(mock_authenticators, "test-deployment")
        result = backend.hcl("test-deployment")
        assert "use_lockfile = true" in result


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

    @mock_aws
    def test_create_bucket_unknown_error(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        with patch.object(
            backend._s3_client,
            "create_bucket",
            side_effect=botocore.exceptions.ClientError(
                {"Error": {"Code": "UnknownError"}}, "CreateBucket"
            ),
        ):
            with pytest.raises(SystemExit):
                backend._create_bucket("test-bucket")


class TestS3BackendEnsureBackendBucket:
    @mock_aws
    def test_ensure_backend_bucket_creates_bucket(
        self, mock_authenticators, mock_click_context
    ):
        delattr(mock_authenticators["aws"], "bucket")
        delattr(mock_authenticators["aws"], "prefix")
        s3 = boto3.client("s3", region_name="us-east-1")
        assert s3.list_buckets()["Buckets"] == []

        S3Backend(mock_authenticators, "test-deployment")
        buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
        assert "test-bucket" in buckets


LOCK_JSON = (
    '{"ID":"0f9c","Operation":"OperationTypeApply","Info":"",'
    '"Who":"runner@pod-1","Version":"1.13.0",'
    '"Created":"2026-11-27T10:00:00.123456789Z","Path":"x"}'
)


class TestS3BackendLocks:
    @mock_aws
    def test_lock_inspection_supported(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        assert backend.lock_inspection is True

    @mock_aws
    def test_get_lock_lockfile(self, mock_authenticators, mocker):
        mocker.patch("tfworker.util.terraform.version_at_least", return_value=True)
        backend = S3Backend(mock_authenticators, "test-deployment")
        assert backend.get_lock("def1") is None
        backend._s3_client.put_object(
            Bucket="test-bucket",
            Key="prefix/def1/terraform.tfstate.tflock",
            Body=LOCK_JSON,
        )
        lock = backend.get_lock("def1")
        assert lock.id == "0f9c"
        assert lock.host == "pod-1"

    @mock_aws
    def test_get_lock_lockfile_unparseable(self, mock_authenticators, mocker):
        mocker.patch("tfworker.util.terraform.version_at_least", return_value=True)
        backend = S3Backend(mock_authenticators, "test-deployment")
        backend._s3_client.put_object(
            Bucket="test-bucket",
            Key="prefix/def1/terraform.tfstate.tflock",
            Body="not json",
        )
        with pytest.raises(BackendError, match="unable to parse s3 lock file"):
            backend.get_lock("def1")

    @mock_aws
    def test_get_lock_read_error(self, mock_authenticators, mocker):
        mocker.patch("tfworker.util.terraform.version_at_least", return_value=True)
        backend = S3Backend(mock_authenticators, "test-deployment")
        error = botocore.exceptions.ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetObject"
        )
        mocker.patch.object(backend._s3_client, "get_object", side_effect=error)
        with pytest.raises(BackendError, match="unable to read s3 object"):
            backend.get_lock("def1")

    @mock_aws
    def test_get_lock_dynamodb(self, mock_authenticators, mocker):
        mocker.patch("tfworker.util.terraform.version_at_least", return_value=False)
        backend = S3Backend(mock_authenticators, "test-deployment")
        assert backend.get_lock("def1") is None
        backend._ddb_client.put_item(
            TableName="terraform-test-deployment",
            Item={
                "LockID": {"S": "test-bucket/prefix/def1/terraform.tfstate"},
                "Info": {"S": LOCK_JSON},
            },
        )
        assert backend.get_lock("def1").operation == "OperationTypeApply"

    @mock_aws
    def test_get_lock_dynamodb_without_info(self, mock_authenticators, mocker):
        mocker.patch("tfworker.util.terraform.version_at_least", return_value=False)
        backend = S3Backend(mock_authenticators, "test-deployment")
        backend._ddb_client.put_item(
            TableName="terraform-test-deployment",
            Item={"LockID": {"S": "test-bucket/prefix/def1/terraform.tfstate"}},
        )
        assert backend.get_lock("def1") is None

    @mock_aws
    def test_get_lock_dynamodb_error(self, mock_authenticators, mocker):
        mocker.patch("tfworker.util.terraform.version_at_least", return_value=False)
        backend = S3Backend(mock_authenticators, "test-deployment")
        error = botocore.exceptions.ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetItem"
        )
        mocker.patch.object(backend._ddb_client, "get_item", side_effect=error)
        with pytest.raises(BackendError, match="unable to read dynamodb lock"):
            backend.get_lock("def1")

    @mock_aws
    def test_lease_round_trip(self, mock_authenticators):
        from datetime import datetime, timezone

        from tfworker.backends.lock import WorkerLease

        backend = S3Backend(mock_authenticators, "test-deployment")
        assert backend.get_lease("def1", "pod-1") is None
        now = datetime(2026, 11, 27, tzinfo=timezone.utc)
        lease = WorkerLease(
            definition="def1",
            action="apply",
            who="runner@pod-1",
            host="pod-1",
            pid=7,
            started_at=now,
            heartbeat_at=now,
            heartbeat_seconds=30,
        )
        backend.put_lease(lease)
        assert backend.get_lease("def1", "pod-1") == lease
        # each host keeps its own lease
        assert backend.get_lease("def1", "pod-2") is None
        keys = [
            o["Key"]
            for o in backend._s3_client.list_objects_v2(
                Bucket="test-bucket", Prefix="prefix/def1/"
            )["Contents"]
        ]
        assert "prefix/def1/terraform.tfstate.tfworker-lease.pod-1" in keys
        backend.delete_lease("def1", "pod-1")
        assert backend.get_lease("def1", "pod-1") is None

    @mock_aws
    def test_get_lease_unreadable(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        backend._s3_client.put_object(
            Bucket="test-bucket",
            Key="prefix/def1/terraform.tfstate.tfworker-lease.pod-1",
            Body="{}",
        )
        assert backend.get_lease("def1", "pod-1") is None

    @mock_aws
    def test_lease_write_errors(self, mock_authenticators, mocker):
        from datetime import datetime, timezone

        from tfworker.backends.lock import WorkerLease

        backend = S3Backend(mock_authenticators, "test-deployment")
        error = botocore.exceptions.ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no"}}, "PutObject"
        )
        mocker.patch.object(backend._s3_client, "put_object", side_effect=error)
        mocker.patch.object(backend._s3_client, "delete_object", side_effect=error)
        now = datetime(2026, 11, 27, tzinfo=timezone.utc)
        lease = WorkerLease(
            definition="def1",
            action="plan",
            who="a@b",
            host="b",
            pid=1,
            started_at=now,
            heartbeat_at=now,
            heartbeat_seconds=30,
        )
        with pytest.raises(BackendError, match="unable to write worker lease"):
            backend.put_lease(lease)
        with pytest.raises(BackendError, match="unable to delete worker lease"):
            backend.delete_lease("def1", "b")

    @mock_aws
    def test_botocore_errors_are_backend_errors(self, mock_authenticators, mocker):
        from datetime import datetime, timezone

        from tfworker.backends.lock import WorkerLease

        backend = S3Backend(mock_authenticators, "test-deployment")
        error = botocore.exceptions.ConnectTimeoutError(endpoint_url="https://s3")
        for name in ("get_object", "put_object", "delete_object"):
            mocker.patch.object(backend._s3_client, name, side_effect=error)
        mocker.patch.object(backend._ddb_client, "get_item", side_effect=error)
        now = datetime(2026, 11, 27, tzinfo=timezone.utc)
        lease = WorkerLease(
            definition="def1",
            action="plan",
            who="a@b",
            host="b",
            pid=1,
            started_at=now,
            heartbeat_at=now,
            heartbeat_seconds=30,
        )
        mocker.patch("tfworker.util.terraform.version_at_least", return_value=True)
        with pytest.raises(BackendError):
            backend.get_lock("def1")
        with pytest.raises(BackendError):
            backend.get_lease("def1", "b")
        with pytest.raises(BackendError):
            backend.put_lease(lease)
        with pytest.raises(BackendError):
            backend.delete_lease("def1", "b")
        mocker.patch("tfworker.util.terraform.version_at_least", return_value=False)
        with pytest.raises(BackendError, match="dynamodb"):
            backend.get_lock("def1")

    @mock_aws
    def test_clean_removes_leftover_lease(self, mock_authenticators, empty_state):
        backend = S3Backend(mock_authenticators, "test-deployment")
        s3 = backend._s3_client
        s3.put_object(
            Bucket="test-bucket", Key="prefix/def1/terraform.tfstate", Body=empty_state
        )
        s3.put_object(
            Bucket="test-bucket",
            Key="prefix/def1/terraform.tfstate.tfworker-lease.pod-1",
            Body="{}",
        )
        backend._clean_bucket_state("def1")
        listing = s3.list_objects_v2(Bucket="test-bucket", Prefix="prefix/def1/")
        assert listing.get("KeyCount", 0) == 0


class TestS3BackendReports:
    @mock_aws
    def test_location(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        location = backend.location("def1")
        assert backend.report_storage is True
        assert location.backend == "s3"
        assert location.account_id == "123456789012"
        assert location.region == "us-east-1"
        assert location.bucket == "test-bucket"
        assert location.state_key == "prefix/def1/terraform.tfstate"
        backend.location("def2")

    @mock_aws
    def test_account_id_lookup_failure(self, mock_authenticators, mocker):
        backend = S3Backend(mock_authenticators, "test-deployment")
        sts = mocker.MagicMock()
        sts.get_caller_identity.side_effect = botocore.exceptions.NoCredentialsError()
        mocker.patch.object(
            mock_authenticators["aws"].backend_session, "client", return_value=sts
        )
        assert backend.location("def1").account_id is None

    @mock_aws
    def test_state_version(self, mock_authenticators):
        backend = S3Backend(mock_authenticators, "test-deployment")
        assert backend.state_version("def1") is None
        backend._s3_client.put_object(
            Bucket="test-bucket",
            Key="prefix/def1/terraform.tfstate",
            Body='{"lineage": "abc", "serial": 7, "resources": []}',
        )
        assert backend.state_version("def1") == ("abc", 7)
        backend._s3_client.put_object(
            Bucket="test-bucket", Key="prefix/def1/terraform.tfstate", Body="{}"
        )
        with pytest.raises(BackendError, match="lineage"):
            backend.state_version("def1")

    @mock_aws
    def test_report_round_trip_and_conditions(self, mock_authenticators):
        from tfworker.exceptions import ReportConflictError

        backend = S3Backend(mock_authenticators, "test-deployment")
        assert backend.read_report("status/def1.json") is None
        backend.write_report("status/def1.json", "one", create_only=True)
        text, etag = backend.read_report("status/def1.json")
        assert text == "one"
        with pytest.raises(ReportConflictError):
            backend.write_report("status/def1.json", "two", create_only=True)
        backend.write_report("status/def1.json", "two", if_match=etag)
        with pytest.raises(ReportConflictError):
            backend.write_report("status/def1.json", "three", if_match=etag)
        assert backend.read_report("status/def1.json")[0] == "two"
        obj = backend._s3_client.get_object(
            Bucket="test-bucket", Key="prefix/_tfworker/status/def1.json"
        )
        assert obj["ContentType"] == "application/json"

    @mock_aws
    def test_report_errors(self, mock_authenticators, mocker):
        backend = S3Backend(mock_authenticators, "test-deployment")
        denied = botocore.exceptions.ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no"}}, "Op"
        )
        timeout = botocore.exceptions.ConnectTimeoutError(endpoint_url="https://s3")
        for error in (denied, timeout):
            mocker.patch.object(backend._s3_client, "get_object", side_effect=error)
            mocker.patch.object(backend._s3_client, "put_object", side_effect=error)
            with pytest.raises(BackendError, match="unable to read report"):
                backend.read_report("x.json")
            with pytest.raises(BackendError, match="unable to write report"):
                backend.write_report("x.json", "{}")

    @mock_aws
    def test_clean_keeps_run_history(self, mock_authenticators, empty_state):
        backend = S3Backend(mock_authenticators, "test-deployment")
        s3 = backend._s3_client
        s3.put_object(
            Bucket="test-bucket", Key="prefix/def1/terraform.tfstate", Body=empty_state
        )
        backend.write_report("runs/2026/11/27/r1.json", "{}")
        backend._clean_bucket_state()
        keys = [
            o["Key"]
            for o in s3.list_objects_v2(Bucket="test-bucket", Prefix="prefix/")[
                "Contents"
            ]
        ]
        assert keys == ["prefix/_tfworker/runs/2026/11/27/r1.json"]
