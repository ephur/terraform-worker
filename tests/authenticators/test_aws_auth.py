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

from unittest.mock import patch

import pytest
from botocore.credentials import Credentials
from moto import mock_aws

from tfworker.authenticators.aws import AWSAuthenticator, MissingArgumentException
from tfworker.commands.root import RootCommand
from tfworker.constants import DEFAULT_BACKEND_PREFIX


@pytest.fixture
def cli_args(aws_access_key_id, aws_secret_access_key):
    return {
        "aws_access_key_id": aws_access_key_id,
        "aws_secret_access_key": aws_secret_access_key,
        "aws_default_region": "us-east-1",
        "backend_prefix": DEFAULT_BACKEND_PREFIX,
    }


@pytest.fixture
def state_args(cli_args):
    result = RootCommand.StateArgs()
    for k, v in cli_args.items():
        setattr(result, k, v)
    setattr(result, "backend_bucket", "alphabet")
    return result


@pytest.fixture
def state_args_with_profile_only():
    result = RootCommand.StateArgs()
    setattr(result, "aws_profile", "testing")
    setattr(result, "backend_bucket", "alphabet")
    return result


@pytest.fixture
def state_args_with_role_arn(state_args, aws_role_arn):
    setattr(state_args, "aws_role_arn", aws_role_arn)
    setattr(state_args, "backend_bucket", "alphabet")
    return state_args


@pytest.fixture
def aws_credentials_instance(state_args):
    return Credentials(state_args.aws_access_key_id, state_args.aws_secret_access_key)


class TestAWSAuthenticator:
    def test_with_no_backend_bucket(self):
        with pytest.raises(MissingArgumentException) as e:
            AWSAuthenticator(state_args={}, deployment="deployfu")
            assert "backend_bucket" in str(e.value)

    @mock_aws
    def test_with_access_key_pair_creds(
        self, sts_client, state_args, aws_access_key_id, aws_secret_access_key
    ):
        auth = AWSAuthenticator(state_args, deployment="deployfu")
        assert auth.access_key_id == aws_access_key_id
        assert auth.secret_access_key == aws_secret_access_key
        assert auth.session_token is None

    @mock_aws
    def test_with_access_key_pair_creds_and_role_arn(
        self, sts_client, state_args_with_role_arn, aws_secret_access_key
    ):
        auth = AWSAuthenticator(state_args_with_role_arn, deployment="deployfu")
        # The access_key_id we retrieve should NOT be the one from the fixture,
        # but rather one that moto generates
        assert auth.access_key_id.startswith("ASIA")
        assert auth.secret_access_key != aws_secret_access_key
        # Taking as a cue: https://github.com/spulec/moto/blob/master/tests/test_sts/test_sts.py#L636
        assert auth.session_token.startswith("FQoGZXIvYXdzE")

    @patch("botocore.session.Session.get_scoped_config")
    @patch("botocore.session.Session.get_credentials")
    def test_with_profile(
        self,
        mocked_credentials,
        mocked_config,
        state_args_with_profile_only,
        aws_access_key_id,
        aws_secret_access_key,
        aws_credentials_instance,
        cli_args,
    ):
        mocked_credentials.return_value = aws_credentials_instance
        mocked_config.return_value = cli_args
        auth = AWSAuthenticator(state_args_with_profile_only, deployment="deployfu")
        assert auth.profile == "testing"
        assert auth.access_key_id == aws_credentials_instance.access_key
        assert auth.secret_access_key == aws_credentials_instance.secret_key
        assert auth.session_token is None

    @mock_aws
    def test_with_prefix(self, state_args):
        auth = AWSAuthenticator(state_args, deployment="deployfu")
        assert auth.prefix == DEFAULT_BACKEND_PREFIX.format(deployment="deployfu")

        state_args.backend_prefix = "my-prefix"
        auth = AWSAuthenticator(state_args, deployment="deployfu")
        assert auth.prefix == "my-prefix"
