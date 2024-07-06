import shlex
from typing import Dict

import boto3
from botocore.credentials import Credentials
from pydantic import model_validator

import tfworker.util.log as log
from tfworker.exceptions import TFWorkerException

from .base import BaseAuthenticator, BaseAuthenticatorConfig


class AWSAuthenticatorConfig(BaseAuthenticatorConfig):
    """
    A configuration that describes the configuration required for the AWS Authenticator.

    This model is populated by values from the "tfworker.cli_options.CLIOptionsRoot" model.

    Attributes:
        aws_region (str): the AWS region to use. This is required.
        aws_access_key_id (str): an aws access key id. Either this or a profile is required.
        aws_external_id (str): a unique ID that can be used for cross account assumptions. Defaults to None.
        aws_profile (str): an aws profile. Either this or an access key id is required.
        aws_role_arn (str): if provided, the role to assume using other creds. Defaults to None.
        aws_secret_access_key (str): an aws secret access key. Either this or a profile is required.
        aws_session_token (str): an aws session token. Defaults to None.
        backend_region (str): the AWS region for the TF backend (s3, dynamodb). Defaults to `aws_region`.
        backend_role_arn (str): the role to assume for the backend. Defaults to None.
        duration (int): the duration of an assumed role session. Defaults to 3600.
        session_name (str): the name of the assumed role session. Defaults to "tfworker".
    """

    aws_region: str
    aws_access_key_id: str | None = None
    aws_external_id: str | None = None
    aws_profile: str | None = None
    aws_role_arn: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None
    backend_region: str | None = None
    backend_role_arn: str | None = None
    duration: int | None = 3600
    mfa_serial: str | None = None
    session_name: str | None = "tfworker"

    @model_validator(mode="before")
    @classmethod
    def set_backend_region(cls, values: Dict[str, str]) -> Dict[str, str]:
        """
        Sets the backend region to the same as the AWS region if not provided

        Args:
            values (dict): the values passed to the model

        Returns:
            dict: the modified values
        """
        if values.get("aws_region") and not values.get("backend_region"):
            values["backend_region"] = values["aws_region"]
        return values

    @model_validator(mode="before")
    @classmethod
    def check_valid_aws_auth(cls, values: Dict[str, str]) -> Dict[str, str]:
        """
        Validates that an acceptable configuration for AWS authentication is provided

        Args:
            values (dict): the values passed to the model

        Returns:
            dict: the unmodified values

        Raises:
            ValueError: if the configuration is not valid
        """

        if not (
            values.get("aws_access_key_id") and values.get("aws_secret_access_key")
        ) and not values.get("aws_profile"):
            raise ValueError(
                "Either aws_access_key_id and aws_secret_access_key or profile must be provided"
            )
        return values


class AWSAuthenticator(BaseAuthenticator):
    """
    The AWS authenticator is used to authenticate to AWS and generate environment variables

    Attributes:
        tag (str): the tag for the authenticator, used by other methods to lookup the authenticator
        config_model (AWSAuthenticatorConfig): the configuration model for the authenticator
        session (boto3.session): the primary session
        backend_session (boto3.session): the backend session
        session_credentials (Dict[str, str]): the credentials for the primary session
        backend_session_credentials (Dict[str, str]): the credentials for the backend session
    """

    tag: str = "aws"
    config_model: BaseAuthenticatorConfig = AWSAuthenticatorConfig

    def __init__(self, auth_config: AWSAuthenticatorConfig) -> None:
        """
        Initialize the AWS authenticator

        Args:
            auth_config (AWSAuthenticatorConfig): the configuration for the authenticator

        Raises:
            TFWorkerException: if there is an error authenticating to AWS
        """
        self._backend_session: boto3.session = None
        self._session: boto3.session = None

        log.debug(f"authenticating to AWS, in region {auth_config.aws_region}")
        # The initial session is used to create any other sessions, or use directly if no role is assumed
        try:
            log.trace("authenticating to AWS for initial session")
            init_session: boto3.session = boto3.Session(
                region_name=auth_config.aws_region,
                **_get_init_session_args(auth_config),
            )
        except Exception as e:
            raise TFWorkerException(f"error authenticating to AWS: {e}") from e

        # handle role assumption if necessary
        if not auth_config.aws_role_arn:
            log.trace("no role to assume, using initial session")
            self._session = init_session
        else:
            log.info(f"assuming role: {auth_config.aws_role_arn}")
            self._session = _assume_role_session(init_session, auth_config)

        # handle backend session if necessary
        if not _need_backend_session(auth_config):
            log.trace("backend session and regular session are the same")
            self._backend_session = self._session
        else:
            log.trace(
                f"gathering backend session in region {auth_config.backend_region}"
            )
            self._backend_session = _get_backend_session(auth_config, init_session)

    @property
    def backend_session(self) -> boto3.session:
        return self._backend_session

    @property
    def backend_session_credentials(self) -> Dict[str, str]:
        return self._backend_session.get_credentials()

    @property
    def session(self) -> boto3.session:
        return self._session

    @property
    def session_credentials(self) -> Dict[str, str]:
        return self._session.get_credentials()

    def env(self, backend: bool = False) -> Dict[str, str]:
        """
        env returns a dictionary of environment variables that should be set

        Args:
            backend (bool): whether this is for the backend. Defaults to False.

        Returns:
            Dict[str, str]: the environment variables
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
            result["AWS_SESSION_TOKEN"] = shlex.quote(creds.token)

        return result


def _assume_role_session(
    session: boto3.session, auth_config: AWSAuthenticatorConfig, backend=False
) -> boto3.session:
    """
    Uses the provided session to assume a role

    Args:
        session (boto3.session): the session to use for the assumption
        backend (bool): whether this is for the backend. Defaults to False.

    Returns:
        boto3.session: the new session

    Raises:
        TFWorkerException: if there is an error assuming the role
    """
    sts_client = session.client("sts")

    if backend:
        assume_args = {
            "RoleArn": auth_config.backend_role_arn,
            "RoleSessionName": auth_config.session_name,
            "DurationSeconds": auth_config.duration,
        }
        region = auth_config.backend_region
    else:
        assume_args = {
            "RoleArn": auth_config.aws_role_arn,
            "RoleSessionName": auth_config.session_name,
            "DurationSeconds": auth_config.duration,
        }
        region = auth_config.aws_region

    if auth_config.aws_external_id:
        assume_args["ExternalId"] = auth_config.aws_external_id

    role_creds = sts_client.assume_role(**assume_args)["Credentials"]
    try:
        new_session = boto3.Session(
            aws_access_key_id=role_creds["AccessKeyId"],
            aws_secret_access_key=role_creds["SecretAccessKey"],
            aws_session_token=role_creds["SessionToken"],
            region_name=region,
        )
    except Exception as e:
        raise TFWorkerException(f"error assuming role: {e}") from e

    return new_session


def _get_backend_session(
    auth_config: AWSAuthenticatorConfig, init_session: boto3.session
) -> boto3.session:
    """
    Gets the backend session

    Args:
        auth_config (AWSAuthenticatorConfig): the configuration for the authenticator
        init_session (boto3.session): the initial session

    Raises:
        TFWorkerException: if there is an error getting the backend session
    """
    try:
        if auth_config.backend_role_arn:
            log.info(f"assuming backend role: {auth_config.backend_role_arn}")
            backend_session = _assume_role_session(
                init_session, auth_config, backend=True
            )
        else:
            log.debug(
                f"authenticating to AWS for backend session, in region {auth_config.backend_region}"
            )
            backend_session = boto3.Session(
                region_name=auth_config.backend_region,
                **_get_init_session_args(auth_config),
            )
    except Exception as e:
        raise TFWorkerException(
            f"error authenticating to AWS for backend session: {e}"
        ) from e

    return backend_session


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


def _need_backend_session(auth_config: AWSAuthenticatorConfig) -> bool:
    """
    Returns whether a backend session is needed

    Args:
        auth_config (AWSAuthenticatorConfig): the configuration for the authenticator

    Returns:
        bool: whether a backend session is needed
    """
    # the conditions in which a backend session is needed:
    # - backend_region is different from aws_region
    # - backend_role_arn is provided
    return (
        auth_config.aws_region != auth_config.backend_region
        or auth_config.backend_role_arn is not None
    )
