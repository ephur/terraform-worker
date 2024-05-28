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

import collections
import os
import random
import string
from unittest import mock

import boto3
import pytest
from moto import mock_aws

import tfworker
import tfworker.commands.base
import tfworker.commands.root
import tfworker.providers


@pytest.fixture
def aws_access_key_id():
    suffix = "".join(random.choices("RAX", k=16))
    return f"AKIA{suffix}"


@pytest.fixture
def aws_secret_access_key():
    return "".join(
        random.choices(
            string.ascii_uppercase + string.ascii_lowercase + string.digits, k=40
        )
    )


@pytest.fixture
def aws_role_arn(aws_account_id, aws_role_name):
    return f"arn:aws:iam:{aws_account_id}:role/{aws_role_name}"


@pytest.fixture
def aws_role_name():
    return "".join(random.choices(string.ascii_lowercase, k=8))


@pytest.fixture
def aws_account_id():
    return "".join(random.choices(string.digits, k=12))


@pytest.fixture(scope="class")
def aws_credentials():
    """Mocked AWS Credentials for moto."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"


@pytest.fixture(scope="class")
def s3_client(aws_credentials):
    with mock_aws():
        yield boto3.client("s3", region_name="us-west-2")


@pytest.fixture(scope="class")
def dynamodb_client(aws_credentials):
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-west-2")


@pytest.fixture(scope="class")
def sts_client(aws_credentials):
    with mock_aws():
        yield boto3.client("sts", region_name="us-west-2")


class MockAWSAuth:
    """
    This class is used to replace the AWS authenticator, moto is unable to
    provide mock support for the complex authentication options we support
    (cross account assumed roles, user identity, etc...)
    """

    @mock_aws
    def __init__(self):
        self._session = boto3.Session()
        self._backend_session = self._session
        self.bucket = "test_bucket"
        self.prefix = "terraform/test-0001"

    @property
    def session(self):
        return self._session

    @property
    def backend_session(self):
        return self._backend_session


@pytest.fixture()
def grootc():
    result = tfworker.commands.root.RootCommand(
        args={
            "backend": "gcs",
            "backend_region": "us-central1",
            "backend_bucket": "test_gcp_bucket",
            "backend_prefix": "terraform/test-0002",
            "backend_use_all_remotes": False,
            "config_file": os.path.join(
                os.path.dirname(__file__), "fixtures", "gcp_test_config.yaml"
            ),
            "gcp_creds_path": "/home/test/test-creds.json",
            "gcp_project": "test_project",
            "gcp_region": "us-west-2b",
            "repository_path": os.path.join(os.path.dirname(__file__), "fixtures"),
            "create_backend_bucket": True,
        }
    )
    return result


@pytest.fixture()
def grootc_no_create_backend_bucket():
    result = tfworker.commands.root.RootCommand(
        args={
            "backend": "gcs",
            "backend_region": "us-central1",
            "backend_bucket": "test_gcp_bucket",
            "backend_prefix": "terraform/test-0002",
            "backend_use_all_remotes": False,
            "config_file": os.path.join(
                os.path.dirname(__file__), "fixtures", "gcp_test_config.yaml"
            ),
            "gcp_creds_path": "/home/test/test-creds.json",
            "gcp_project": "test_project",
            "gcp_region": "us-west-2b",
            "repository_path": os.path.join(os.path.dirname(__file__), "fixtures"),
            "create_backend_bucket": False,
        }
    )
    return result


@pytest.fixture(scope="function")
@mock.patch("tfworker.authenticators.aws.AWSAuthenticator", new=MockAWSAuth)
def rootc(s3_client, dynamodb_client, sts_client, create_backend_bucket=True):
    result = tfworker.commands.root.RootCommand(
        args={
            "aws_access_key_id": "1234567890",
            "aws_secret_access_key": "1234567890",
            "aws_region": "us-west-2",
            "backend": "s3",
            "backend_region": "us-west-2",
            "backend_bucket": "test_bucket",
            "backend_prefix": "terraform/test-0001",
            "backend_use_all_remotes": False,
            "config_file": os.path.join(
                os.path.dirname(__file__), "fixtures", "test_config.yaml"
            ),
            "gcp_creds_path": "/home/test/test-creds.json",
            "gcp_project": "test_project",
            "gcp_region": "us-west-2b",
            "repository_path": os.path.join(os.path.dirname(__file__), "fixtures"),
            "create_backend_bucket": create_backend_bucket,
        }
    )
    return result


@pytest.fixture(scope="function")
@mock.patch("tfworker.authenticators.aws.AWSAuthenticator", new=MockAWSAuth)
def rootc_no_create_backend_bucket(s3_client, dynamodb_client, sts_client):
    result = tfworker.commands.root.RootCommand(
        args={
            "aws_access_key_id": "1234567890",
            "aws_secret_access_key": "1234567890",
            "aws_region": "us-west-2",
            "backend": "s3",
            "backend_region": "us-west-2",
            "backend_bucket": "test_bucket",
            "backend_prefix": "terraform/test-0001",
            "backend_use_all_remotes": False,
            "config_file": os.path.join(
                os.path.dirname(__file__), "fixtures", "test_config.yaml"
            ),
            "gcp_creds_path": "/home/test/test-creds.json",
            "gcp_project": "test_project",
            "gcp_region": "us-west-2b",
            "repository_path": os.path.join(os.path.dirname(__file__), "fixtures"),
            "create_backend_bucket": False,
        }
    )
    return result


@pytest.fixture(scope="function")
@mock.patch("tfworker.authenticators.aws.AWSAuthenticator", new=MockAWSAuth)
def json_base_rootc(s3_client, dynamodb_client, sts_client):
    result = tfworker.commands.root.RootCommand(
        args={
            "aws_access_key_id": "1234567890",
            "aws_secret_access_key": "1234567890",
            "aws_region": "us-west-2",
            "backend": "s3",
            "backend_region": "us-west-2",
            "backend_bucket": "test_bucket",
            "backend_prefix": "terraform/test-0001",
            "backend_use_all_remotes": False,
            "config_file": os.path.join(
                os.path.dirname(__file__), "fixtures", "base_config_test.json"
            ),
            "gcp_creds_path": "/home/test/test-creds.json",
            "gcp_project": "test_project",
            "gcp_region": "us-west-2b",
            "repository_path": os.path.join(os.path.dirname(__file__), "fixtures"),
        }
    )
    return result


@pytest.fixture(scope="function")
@mock.patch("tfworker.authenticators.aws.AWSAuthenticator", new=MockAWSAuth)
def yaml_base_rootc(s3_client, dynamodb_client, sts_client):
    result = tfworker.commands.root.RootCommand(
        args={
            "aws_access_key_id": "1234567890",
            "aws_secret_access_key": "1234567890",
            "aws_region": "us-west-2",
            "backend": "s3",
            "backend_region": "us-west-2",
            "backend_bucket": "test_bucket",
            "backend_prefix": "terraform/test-0001",
            "backend_use_all_remotes": False,
            "config_file": os.path.join(
                os.path.dirname(__file__), "fixtures", "base_config_test.yaml"
            ),
            "gcp_creds_path": "/home/test/test-creds.json",
            "gcp_project": "test_project",
            "gcp_region": "us-west-2b",
            "repository_path": os.path.join(os.path.dirname(__file__), "fixtures"),
        }
    )
    return result


@pytest.fixture(scope="function")
@mock.patch("tfworker.authenticators.aws.AWSAuthenticator", new=MockAWSAuth)
def hcl_base_rootc(s3_client, dynamodb_client, sts_client):
    result = tfworker.commands.root.RootCommand(
        args={
            "aws_access_key_id": "1234567890",
            "aws_secret_access_key": "1234567890",
            "aws_region": "us-west-2",
            "backend": "s3",
            "backend_region": "us-west-2",
            "backend_bucket": "test_bucket",
            "backend_prefix": "terraform/test-0001",
            "backend_use_all_remotes": False,
            "config_file": os.path.join(
                os.path.dirname(__file__), "fixtures", "base_config_test.hcl"
            ),
            "gcp_creds_path": "/home/test/test-creds.json",
            "gcp_project": "test_project",
            "gcp_region": "us-west-2b",
            "repository_path": os.path.join(os.path.dirname(__file__), "fixtures"),
        }
    )
    return result


@pytest.fixture(scope="function")
@mock.patch("tfworker.authenticators.aws.AWSAuthenticator", new=MockAWSAuth)
def rootc_options(s3_client, dynamodb_client, sts_client):
    result = tfworker.commands.root.RootCommand(
        args={
            "aws_region": "us-east-2",
            "backend": "gcs",
            "backend_region": "us-west-2",
            "backend_bucket": "test_bucket",
            "backend_prefix": "terraform/test-0001",
            "backend_use_all_remotes": False,
            "config_file": os.path.join(
                os.path.dirname(__file__), "fixtures", "test_config_with_options.yaml"
            ),
            "gcp_creds_path": "/home/test/test-creds.json",
            "gcp_project": "test_project",
            "gcp_region": "us-west-2b",
            "repository_path": os.path.join(os.path.dirname(__file__), "fixtures"),
        }
    )
    return result


@pytest.fixture
def basec(rootc, s3_client):
    with mock.patch(
        "tfworker.commands.base.BaseCommand.get_terraform_version",
        side_effect=lambda x: (13, 3),
    ):
        with mock.patch(
            "tfworker.commands.base.which",
            side_effect=lambda x: "/usr/local/bin/terraform",
        ):
            return tfworker.commands.base.BaseCommand(
                rootc, "test-0001", tf_version_major=13
            )


@pytest.fixture
def gbasec(grootc):
    with mock.patch(
        "tfworker.commands.base.BaseCommand.get_terraform_version",
        side_effect=lambda x: (13, 3),
    ):
        with mock.patch(
            "tfworker.commands.base.which",
            side_effect=lambda x: "/usr/local/bin/terraform",
        ):
            with mock.patch(
                "tfworker.backends.gcs.storage.Client.from_service_account_json"
            ):
                return tfworker.commands.base.BaseCommand(
                    grootc, "test-0001", tf_version_major=13
                )


@pytest.fixture
def tf_Xcmd(rootc):
    return tfworker.commands.terraform.TerraformCommand(rootc, deployment="test-0001")


@pytest.fixture
def tf_15cmd(rootc):
    with mock.patch(
        "tfworker.commands.base.BaseCommand.get_terraform_version",
        side_effect=lambda x: (15, 0),
    ):
        with mock.patch(
            "tfworker.commands.base.which",
            side_effect=lambda x: "/usr/local/bin/terraform",
        ):
            return tfworker.commands.terraform.TerraformCommand(
                rootc, deployment="test-0001", tf_version=(15, 0)
            )


@pytest.fixture
def tf_14cmd(rootc):
    with mock.patch(
        "tfworker.commands.base.BaseCommand.get_terraform_version",
        side_effect=lambda x: (14, 5),
    ):
        with mock.patch(
            "tfworker.commands.base.which",
            side_effect=lambda x: "/usr/local/bin/terraform",
        ):
            return tfworker.commands.terraform.TerraformCommand(
                rootc, deployment="test-0001", tf_version=(14, 5)
            )


@pytest.fixture
def tf_13cmd(rootc):
    with mock.patch(
        "tfworker.commands.base.BaseCommand.get_terraform_version",
        side_effect=lambda x: (13, 5),
    ):
        with mock.patch(
            "tfworker.commands.base.which",
            side_effect=lambda x: "/usr/local/bin/terraform",
        ):
            return tfworker.commands.terraform.TerraformCommand(
                rootc, deployment="test-0001", tf_version=(13, 5)
            )


@pytest.fixture
def tf_12cmd(rootc):
    with mock.patch(
        "tfworker.commands.base.BaseCommand.get_terraform_version",
        side_effect=lambda x: (12, 27),
    ):
        with mock.patch(
            "tfworker.commands.base.which",
            side_effect=lambda x: "/usr/local/bin/terraform",
        ):
            return tfworker.commands.terraform.TerraformCommand(
                rootc, deployment="test-0001", tf_version=(12, 27)
            )


@pytest.fixture
def tf_13cmd_options(rootc_options):
    with mock.patch(
        "tfworker.commands.base.BaseCommand.get_terraform_version",
        side_effect=lambda x: (13, 5),
    ):
        with mock.patch(
            "tfworker.commands.base.which",
            side_effect=lambda x: "/usr/local/bin/terraform",
        ):
            with mock.patch(
                "tfworker.backends.gcs.storage.Client.from_service_account_json"
            ):
                return tfworker.commands.terraform.TerraformCommand(
                    rootc_options,
                    deployment="test-0001-options",
                    tf_version=(13, 5),
                    b64_encode=False,
                )


@pytest.fixture
def definition_odict():
    one_def = {
        "test": dict(
            {
                "path": "/test",
                "remote_vars": {"a": 1, "b": "two"},
                "terraform_vars": {"c": 3, "d": "four"},
                "template_vars": {"e": 5, "f": "six"},
            }
        )
    }
    return dict(one_def)


@pytest.fixture
def test_config_file():
    return os.path.join(os.path.dirname(__file__), "fixtures", "test_config.yaml")
