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

import shlex
from typing import Dict

import boto3
from botocore.credentials import Credentials
from pydantic import field_validator, model_validator

import tfworker.util.log as log
from tfworker import constants as const

from .base import BaseAuthenticator, BaseAuthenticatorConfig


class AWSAuthenticatorConfig(BaseAuthenticatorConfig):
    aws_region: str  # the AWS region to use
    aws_access_key_id: str | None = None  # an aws access key id
    aws_external_id: str | None = (
        None  # a unique ID that can be used for cross account assumptions
    )
    aws_profile: str | None = None  # an aws profile
    aws_role_arn: str | None = None  # if provided, the role to assume using other creds
    aws_secret_access_key: str | None = None  # an aws secret access key
    aws_session_token: str | None = None  # an aws session token
    backend_region: str | None = (
        None  # the AWS region for the TF backend (s3, dynamodb)
    )
    backend_role_arn: str | None = None  # the role to assume for the backend
    duration: int | None = 3600  # the duration of an assumed role session
    mfa_serial: str | None = (
        None  # the serial number or ARN of an MFA device ; not yet implemented
    )
    session_name: str | None = "tfworker"  # the name of the assumed role session

    @model_validator(mode="before")
    @classmethod
    def set_backend_region(cls, values):
        if values.get("aws_region") and not values.get("backend_region"):
            values["backend_region"] = values["aws_region"]
        return values

    @model_validator(mode="before")
    @classmethod
    def check_valid_aws_auth(cls, values):
        if not (
            values.get("aws_access_key_id") and values.get("aws_secret_access_key")
        ) and not values.get("aws_profile"):
            raise ValueError(
                "Either aws_access_key_id and aws_secret_access_key or profile must be provided"
            )
        return values

    @field_validator("mfa_serial")
    @classmethod
    def validate_mfa_serial(cls, value):
        if value is not None:
            raise ValueError("MFA is not yet implemented")


class AWSAuthenticator(BaseAuthenticator):
    tag = "aws"
    config_model = AWSAuthenticatorConfig

    def __init__(self, auth_config: AWSAuthenticatorConfig):
        """Initialize the AWS authenticator

        Args:
            auth_config (AWSAuthenticatorConfig): the configuration for the authenticator
        """
        self._backend_session: boto3.session = None
        self._session: boto3.session = None

        log.debug(f"authenticating to AWS, in region {auth_config.aws_region}")
        init_session: boto3.session = boto3.Session(
            region_name=auth_config.aws_region, **_get_init_session_args(auth_config)
        )

        # Handle the primary session
        if not auth_config.aws_role_arn:
            self._session = init_session
        else:
            log.info(f"assuming role: {auth_config.aws_role_arn}")
            self._session = self._assume_role_session(init_session, auth_config)

        # Handle the backend session
        if auth_config.aws_region == auth_config.backend_region:
            self._backend_session = self._session
            log.trace("backend session and regular session are the same")
        else:
            if auth_config.backend_role_arn:
                log.info(f"assuming backend role: {auth_config.backend_role_arn}")
                self._backend_session = self._assume_role_session(
                    init_session, auth_config, backend=True
                )
            else:
                log.debug(
                    f"authenticating to AWS for backend session, in region {auth_config.backend_region}"
                )
                self._backend_session = boto3.Session(
                    region_name=auth_config.backend_region,
                    **_get_init_session_args(auth_config),
                )

    @property
    def backend_session(self) -> boto3.session:
        log.trace(f"returning backend session from {__name__}")
        return self._backend_session

    @property
    def backend_session_credentials(self) -> Dict[str, str]:
        log.trace(f"returning backend session credentials from {__name__}")
        if self._backend_session is None:
            return None
        return self._backend_session.get_credentials()

    @property
    def session(self) -> boto3.session:
        log.trace(f"returning session from {__name__}")
        return self._session

    @property
    def session_credentials(self) -> Dict[str, str]:
        log.trace(f"returning session credentials from {__name__}")
        if self._session is None:
            return None
        return self._session.get_credentials()

    def env(self, backend: bool = False) -> Dict[str, str]:
        """
        env returns a dictionary of environment variables that should be set
        """
        result = {}

        if backend:
            session_ref = self.backend_session
        else:
            session_ref = self.session

        creds: Credentials = session_ref.get_credentials()
        result["AWS_DEFAULT_REGION"] = shlex.quote(session_ref.region_name)
        result["AWS_ACCESS_KEY_ID"] = shlex.quote(creds.access_key)
        result["AWS_SECRET_ACCESS_KEY"] = shlex.quote(creds.secret_key)

        if creds.token:
            result["AWS_SESSION_TOKEN"] = shlex(creds.token)

        return result


def _get_init_session_args(auth_config: AWSAuthenticatorConfig) -> Dict[str, str]:
    """
    Returns a dictionary of arguments to pass to the initial boto3 session

    Args:
        auth_config (AWSAuthenticatorConfig): the configuration for the authenticator

    Returns:
        Dict[str, str]: the arguments to pass to the session
    """
    session_args = dict()

    if auth_config.aws_profile is not None:
        session_args["profile_name"] = auth_config.aws_profile

    if auth_config.aws_access_key_id is not None:
        session_args["aws_access_key_id"] = auth_config.aws_access_key_id

    if auth_config.aws_secret_access_key is not None:
        session_args["aws_secret_access_key"] = auth_config.aws_secret_access_key

    if auth_config.aws_session_token is not None:
        session_args["aws_session_token"] = auth_config.aws_session_token

    return session_args


def _assume_role_session(
    session: boto3.session, auth_config: AWSAuthenticatorConfig, backend=False
) -> boto3.session:
    """
    Uses the provided session to assume a role

    Args:
        session (boto3.session): the session to use for the assumption
        backend (bool): whether this is for the backend

    Returns:
        boto3.session: the new session
    """
    sts_client = session.client("sts")

    if backend:
        assume_args = {
            "RoleArn": auth_config.aws_role_arn,
            "RoleSessionName": auth_config.session_name,
            "DurationSeconds": auth_config.duration,
        }
        region = auth_config.aws_region
    else:
        assume_args = {
            "RoleArn": auth_config.backend_role_arn,
            "RoleSessionName": auth_config.session_name,
            "DurationSeconds": auth_config.duration,
        }
        region = auth_config.backend_region

    if external_id:
        assume_args["ExternalId"] = external_id

    role_creds = sts_client.assume_role(**assume_args)["Credentials"]
    new_session = boto3.Session(
        aws_access_key_id=role_creds["AccessKeyId"],
        aws_secret_access_key=role_creds["SecretAccessKey"],
        aws_session_token=role_creds["SessionToken"],
        region_name=region,
    )

    return new_session
