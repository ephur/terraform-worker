from unittest.mock import MagicMock, patch

import boto3.session
import pytest
from botocore.exceptions import NoCredentialsError
from moto import mock_aws
from pydantic import ValidationError

from tfworker.authenticators import AWSAuthenticator, AWSAuthenticatorConfig
from tfworker.authenticators.aws import (
    _assume_role_session,
    _get_backend_session,
    _get_init_session_args,
    _need_backend_session,
)
from tfworker.constants import DEFAULT_AWS_REGION
from tfworker.exceptions import TFWorkerException

# Mock AWS credentials
MOCK_AWS_CREDS = {
    "aws_region": "us-east-1",
    "aws_access_key_id": "AKIAEXAMPLE",
    "aws_secret_access_key": "SECRETEXAMPLE",
}


@pytest.fixture
def aws_auth_config():
    return AWSAuthenticatorConfig(
        aws_role_arn="arn:aws:iam::123456789012:role/testRole",
        aws_access_key_id="AKIAEXAMPLE",
        aws_secret_access_key="SECRETEXAMPLE",
        aws_region="us-east-1",
        backend_region="us-west-2",
        backend_role_arn="arn:aws:iam::210987654321:role/backendTestRole",
        aws_external_id="123456789012",
    )


@pytest.fixture
def boto_session():
    return boto3.Session(region_name="us-east-1")


class TestAWSAuthenticatorConfig:
    def test_valid_config(self):
        config = AWSAuthenticatorConfig(**MOCK_AWS_CREDS)
        assert config.aws_region == MOCK_AWS_CREDS["aws_region"]
        assert config.aws_access_key_id == MOCK_AWS_CREDS["aws_access_key_id"]
        assert config.aws_secret_access_key == MOCK_AWS_CREDS["aws_secret_access_key"]

    def test_invalid_config_missing_region(self):
        creds_copy = MOCK_AWS_CREDS.copy()
        del creds_copy["aws_region"]
        with pytest.raises(ValidationError) as e:
            AWSAuthenticatorConfig(**creds_copy)

        assert "Field required" in str(e.value)
        assert "aws_region" in str(e.value)

    def test_invalid_config_missing_field(self):
        with pytest.raises(ValidationError):
            AWSAuthenticatorConfig(
                aws_access_key_id="valid", aws_secret_access_key="valid"
            )

    def test_valid_config_with_profile(self):
        config = AWSAuthenticatorConfig(aws_region="us-east-1", aws_profile="default")
        assert config.aws_profile == "default"

    def test_invalid_config_no_creds_or_profile(self):
        with pytest.raises(ValidationError):
            AWSAuthenticatorConfig(aws_region="us-east-1")


class TestAWSAuthenticator:
    @mock_aws
    def test_authenticate_success(self):
        config = AWSAuthenticatorConfig(**MOCK_AWS_CREDS)
        authenticator = AWSAuthenticator(config)
        assert authenticator.session is not None
        assert authenticator.session == authenticator.backend_session

    @patch("boto3.Session", side_effect=NoCredentialsError)
    def test_authenticate_failure_invalid_credentials(self, mock_session):
        mock_call_args = MOCK_AWS_CREDS.copy()
        del mock_call_args["aws_region"]
        mock_call_args["region_name"] = DEFAULT_AWS_REGION
        config = AWSAuthenticatorConfig(**MOCK_AWS_CREDS)
        with pytest.raises(TFWorkerException):
            AWSAuthenticator(config)

        mock_session.assert_called_once_with(**mock_call_args)

    @mock_aws
    def test_env_success(self):
        config = AWSAuthenticatorConfig(**MOCK_AWS_CREDS)
        authenticator = AWSAuthenticator(config)
        env_vars = authenticator.env()
        assert env_vars["AWS_DEFAULT_REGION"] == "us-east-1"
        assert env_vars["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLE"
        assert env_vars["AWS_SECRET_ACCESS_KEY"] == "SECRETEXAMPLE"

    @mock_aws
    def test_env_backend(self):
        config = AWSAuthenticatorConfig(**MOCK_AWS_CREDS)
        authenticator = AWSAuthenticator(config)
        env_vars = authenticator.env(backend=True)
        assert env_vars["AWS_DEFAULT_REGION"] == "us-east-1"
        assert env_vars["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLE"
        assert env_vars["AWS_SECRET_ACCESS_KEY"] == "SECRETEXAMPLE"

    @mock_aws
    def test_env_with_assumed_role(self):
        role_creds = MOCK_AWS_CREDS.copy()
        role_creds.update({"aws_role_arn": "arn:aws:iam::123456789012:role/TestRole"})
        config = AWSAuthenticatorConfig(**role_creds)
        authenticator = AWSAuthenticator(config)
        env_vars = authenticator.env()
        assert len(env_vars) == 4
        assert env_vars["AWS_DEFAULT_REGION"] == "us-east-1"
        assert env_vars.get("AWS_ACCESS_KEY_ID") is not None
        assert env_vars.get("AWS_SECRET_ACCESS_KEY") is not None
        assert env_vars.get("AWS_SESSION_TOKEN") is not None

    @mock_aws
    def test_session_and_credentials(self):
        from botocore.credentials import Credentials

        config = AWSAuthenticatorConfig(**MOCK_AWS_CREDS)
        authenticator = AWSAuthenticator(config)
        assert authenticator.session is not None
        creds = authenticator.session_credentials
        assert type(creds) is Credentials

    @mock_aws
    def test_backend_session_and_credentials(self):
        from botocore.credentials import Credentials

        config = AWSAuthenticatorConfig(**MOCK_AWS_CREDS)
        authenticator = AWSAuthenticator(config)
        assert authenticator.backend_session is not None
        creds = authenticator.backend_session_credentials
        assert type(creds) is Credentials

    @mock_aws
    def test_authenticate_with_role_arn(self):
        role_creds = MOCK_AWS_CREDS.copy()
        role_creds.update({"aws_role_arn": "arn:aws:iam::123456789012:role/TestRole"})
        config = AWSAuthenticatorConfig(**role_creds)
        authenticator = AWSAuthenticator(config)
        assert authenticator.session is not None
        assert authenticator.backend_session is not None

    @mock_aws
    def test_authenticate_backend_different_region(self):
        role_creds = MOCK_AWS_CREDS.copy()
        role_creds.update({"backend_region": "us-west-2"})
        config = AWSAuthenticatorConfig(**role_creds)
        assert config.backend_region == "us-west-2"
        authenticator = AWSAuthenticator(config)
        assert authenticator.session is not None
        assert authenticator.backend_session is not None
        assert authenticator.session != authenticator.backend_session
        assert authenticator.backend_session.region_name == "us-west-2"

    @mock_aws
    def test_authenticate_backend_different_role(self):
        role_creds = MOCK_AWS_CREDS.copy()
        role_creds.update(
            {"backend_role_arn": "arn:aws:iam::123456789012:role/TestRole"}
        )
        config = AWSAuthenticatorConfig(**role_creds)
        authenticator = AWSAuthenticator(config)
        assert authenticator.session is not None
        assert authenticator.backend_session is not None
        assert authenticator.session != authenticator.backend_session

    @mock_aws
    def test_backend_region_property(self):
        alt_creds = MOCK_AWS_CREDS.copy()
        alt_creds.update({"backend_region": "us-west-2"})
        config = AWSAuthenticatorConfig(**alt_creds)
        authenticator = AWSAuthenticator(config)
        assert authenticator.backend_region == "us-west-2"

    @mock_aws
    def test_backend_region_property_default(self):
        config = AWSAuthenticatorConfig(**MOCK_AWS_CREDS)
        authenticator = AWSAuthenticator(config)
        assert authenticator.backend_region == "us-east-1"

    @mock_aws
    def test_region_property(self):
        alt_creds = MOCK_AWS_CREDS.copy()
        alt_creds.update({"aws_region": "us-west-2"})
        config = AWSAuthenticatorConfig(**alt_creds)
        authenticator = AWSAuthenticator(config)
        assert authenticator.region == "us-west-2"

    @mock_aws
    def test_region_property_default(self):
        config = AWSAuthenticatorConfig(**MOCK_AWS_CREDS)
        authenticator = AWSAuthenticator(config)
        assert authenticator.region == "us-east-1"


@mock_aws
class TestAssumeRoleSession:
    def test_assume_role_for_backend(self, boto_session, aws_auth_config):
        """Test assuming a role for the backend."""
        new_session = _assume_role_session(boto_session, aws_auth_config, backend=True)
        assert new_session.region_name == aws_auth_config.backend_region

    def test_assume_role_not_for_backend(self, boto_session, aws_auth_config):
        """Test assuming a role not for the backend."""
        new_session = _assume_role_session(boto_session, aws_auth_config, backend=False)
        assert new_session.region_name == aws_auth_config.aws_region

    def test_assume_role_with_external_id(self, boto_session, aws_auth_config):
        """Test assuming a role with an external ID."""
        new_session = _assume_role_session(boto_session, aws_auth_config, backend=False)
        assert new_session is not None

    @patch("boto3.Session", side_effect=NoCredentialsError)
    def test_assume_role_failure_raises_exception(self, boto_session, aws_auth_config):
        """Test that an exception is raised if assuming the role fails."""
        with pytest.raises(TFWorkerException):
            _assume_role_session(boto_session, aws_auth_config, backend=False)


class TestGetBackendSession:
    @patch("tfworker.authenticators.aws._assume_role_session")
    @patch("boto3.Session")
    def test_get_backend_session_with_role_arn(
        self, mock_boto3_session, mock_assume_role_session
    ):
        """Test getting backend session with backend_role_arn provided."""
        auth_config = AWSAuthenticatorConfig(
            **MOCK_AWS_CREDS,
            backend_role_arn="arn:aws:iam::123456789012:role/backendRole",
            backend_region="us-west-2"
        )
        init_session = MagicMock()
        mock_assume_role_session.return_value = MagicMock()

        _get_backend_session(auth_config, init_session)

        mock_assume_role_session.assert_called_once_with(
            init_session, auth_config, backend=True
        )

    @patch("tfworker.authenticators.aws._assume_role_session")
    @patch("boto3.Session")
    def test_get_backend_session_without_role_arn(
        self, mock_boto3_session, mock_assume_role_session
    ):
        """Test getting backend session without backend_role_arn provided."""
        auth_config = AWSAuthenticatorConfig(
            **MOCK_AWS_CREDS, backend_role_arn=None, backend_region="us-west-2"
        )
        init_session = MagicMock()
        mock_boto3_session.return_value = MagicMock()

        _get_backend_session(auth_config, init_session)

        mock_boto3_session.assert_called_once_with(
            region_name="us-west-2",
            aws_access_key_id=MOCK_AWS_CREDS["aws_access_key_id"],
            aws_secret_access_key=MOCK_AWS_CREDS["aws_secret_access_key"],
        )
        mock_assume_role_session.assert_not_called()

    @patch("tfworker.authenticators.aws._assume_role_session")
    @patch("boto3.Session")
    def test_get_backend_session_raises_exception(
        self, mock_boto3_session, mock_assume_role_session
    ):
        """Test that TFWorkerException is raised when there's an error getting the backend session."""
        auth_config = AWSAuthenticatorConfig(
            **MOCK_AWS_CREDS, backend_role_arn="invalid", backend_region="us-west-2"
        )
        init_session = MagicMock()
        mock_assume_role_session.side_effect = Exception("Test error")

        with pytest.raises(TFWorkerException):
            _get_backend_session(auth_config, init_session)

        mock_boto3_session.assert_not_called()


class TestGetInitSessionArgs:
    def test_with_aws_profile(self):
        """Test with only aws_profile provided."""
        auth_config = AWSAuthenticatorConfig(
            aws_profile="test_profile", aws_region="us-east-1"
        )
        expected = {"profile_name": "test_profile"}
        assert _get_init_session_args(auth_config) == expected

    def test_with_access_key_and_secret_key(self):
        """Test with aws_access_key_id and aws_secret_access_key provided."""
        auth_config = AWSAuthenticatorConfig(
            aws_access_key_id="test_id",
            aws_secret_access_key="test_secret",
            aws_region="us-east-1",
        )
        expected = {
            "aws_access_key_id": "test_id",
            "aws_secret_access_key": "test_secret",
        }
        assert _get_init_session_args(auth_config) == expected

    def test_with_all_parameters(self):
        """Test with all parameters provided."""
        auth_config = AWSAuthenticatorConfig(
            aws_profile="test_profile",
            aws_access_key_id="test_id",
            aws_secret_access_key="test_secret",
            aws_session_token="test_token",
            aws_region="us-east-1",
        )
        expected = {
            "profile_name": "test_profile",
            "aws_access_key_id": "test_id",
            "aws_secret_access_key": "test_secret",
            "aws_session_token": "test_token",
        }
        assert _get_init_session_args(auth_config) == expected


class TestNeedBackendSession:
    def test_backend_session_not_needed(self):
        """Test that a backend session is not needed when regions are the same and no backend_role_arn."""
        auth_config = AWSAuthenticatorConfig(
            aws_region="us-east-1", aws_profile="test_profile"
        )
        assert not _need_backend_session(auth_config)

    def test_backend_session_needed_different_regions(self):
        """Test that a backend session is needed when aws_region and backend_region are different."""
        auth_config = AWSAuthenticatorConfig(
            aws_region="us-east-1",
            aws_profile="test_profile",
            backend_region="us-west-2",
        )
        assert _need_backend_session(auth_config)

    def test_backend_session_needed_with_backend_role_arn(self):
        """Test that a backend session is needed when backend_role_arn is provided."""
        auth_config = AWSAuthenticatorConfig(
            aws_region="us-east-1",
            aws_profile="test_profile",
            backend_region="us-east-1",
            backend_role_arn="arn:aws:iam::123456789012:role/backendRole",
        )
        assert _need_backend_session(auth_config)
