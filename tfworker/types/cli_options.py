import os
from pathlib import Path
from typing import List, Optional, Union

from pydantic import BaseModel, Field, field_validator, root_validator

from tfworker import constants as const


class CLIOptionsRoot(BaseModel):
    aws_access_key_id: Optional[str] = Field(
        None, env="AWS_ACCESS_KEY_ID", description="AWS Access key"
    )
    aws_secret_access_key: Optional[str] = Field(
        None, env="AWS_SECRET_ACCESS_KEY", description="AWS access key secret"
    )
    aws_session_token: Optional[str] = Field(
        None, env="AWS_SESSION_TOKEN", description="AWS access key token"
    )
    aws_role_arn: Optional[str] = Field(
        None,
        env="AWS_ROLE_ARN",
        description="If provided, credentials will be used to assume this role (complete ARN)",
    )
    aws_external_id: Optional[str] = Field(
        None,
        env="AWS_EXTERNAL_ID",
        description="If provided, will be used to assume the role specified by --aws-role-arn",
    )
    aws_region: str = Field(
        const.DEFAULT_AWS_REGION,
        env="AWS_DEFAULT_REGION",
        description="AWS Region to build in",
    )
    aws_profile: Optional[str] = Field(
        None, env="AWS_PROFILE", description="The AWS/Boto3 profile to use"
    )
    gcp_region: str = Field(
        const.DEFAULT_GCP_REGION, env="GCP_REGION", description="Region to build in"
    )
    gcp_creds_path: Optional[str] = Field(
        None,
        env="GCP_CREDS_PATH",
        description="Relative path to the credentials JSON file for the service account to be used.",
    )
    gcp_project: Optional[str] = Field(
        None,
        env="GCP_PROJECT",
        description="GCP project name to which work will be applied",
    )
    config_file: str = Field(
        const.DEFAULT_CONFIG,
        env="WORKER_CONFIG_FILE",
        description="Path to the configuration file",
        required=True,
    )
    repository_path: str = Field(
        const.DEFAULT_REPOSITORY_PATH,
        env="WORKER_REPOSITORY_PATH",
        description="The path to the terraform module repository",
        required=True,
    )
    backend: Optional[str] = Field(
        None,
        env="WORKER_BACKEND",
        description="State/locking provider. One of: s3, gcs",
    )
    backend_bucket: Optional[str] = Field(
        None,
        env="WORKER_BACKEND_BUCKET",
        description="Bucket (must exist) where all terraform states are stored",
    )
    backend_prefix: str = Field(
        const.DEFAULT_BACKEND_PREFIX,
        env="WORKER_BACKEND_PREFIX",
        description="Prefix to use in backend storage bucket for all terraform states",
    )
    backend_region: str = Field(
        const.DEFAULT_AWS_REGION,
        description="Region where terraform root/lock bucket exists",
    )
    backend_use_all_remotes: bool = Field(
        True,
        env="WORKER_BACKEND_USE_ALL_REMOTES",
        description="Generate remote data sources based on all definition paths present in the backend",
    )
    create_backend_bucket: bool = Field(
        True, description="Create the backend bucket if it does not exist"
    )
    config_var: Optional[List[str]] = Field(
        [],
        description='key=value to be supplied as jinja variables in config_file under "var" dictionary, can be specified multiple times',
    )
    working_dir: Optional[str] = Field(
        None,
        env="WORKER_WORKING_DIR",
        description="Specify the path to use instead of a temporary directory, must exist, be empty, and be writeable, --clean applies to this directory as well",
    )
    clean: Optional[bool] = Field(
        None,
        env="WORKER_CLEAN",
        description="Clean up the temporary directory created by the worker after execution",
    )
    backend_plans: bool = Field(
        False, env="WORKER_BACKEND_PLANS", description="Store plans in the backend"
    )

    @root_validator(pre=True)
    def set_default_clean(cls, values):
        if values.get("working_dir") is not None:
            if "clean" not in values or values["clean"] is None:
                values["clean"] = False
        else:
            if "clean" not in values or values["clean"] is None:
                values["clean"] = True
        return values

    @field_validator("working_dir")
    @classmethod
    def validate_working_dir(cls, fpath: Union[str, None]) -> Union[str, None]:
        """Validate the working directory path.

        Args:
            fpath: Path to the working directory.

        Returns:
            Path to the working directory.

        Raises:
            ValueError: If the path does not exist, is not a directory, or is not empty.
        """
        if fpath is None:
            return
        with Path(fpath) as wpath:
            if not wpath.exists():
                raise ValueError(f"Working path {fpath} does not exist!")
            if not wpath.is_dir():
                raise ValueError(f"Working path {fpath} is not a directory!")
            if any(wpath.iterdir()):
                raise ValueError(f"Working path {fpath} must be empty!")
        return fpath

    @field_validator("gcp_creds_path")
    @classmethod
    def validate_gcp_creds_path(cls, fpath: Union[str, None]) -> Union[str, None]:
        """Validate the GCP credentials path.

        Args:
            fpath: Path to the GCP credentials file.

        Returns:
            Fully resolved path to the GCP credentials file.

        Raises:
            ValueError: If the path does not exist or is not a file.
        """
        if fpath is None:
            return
        if not os.path.isabs(fpath):
            fpath = os.path.abspath(fpath)
        if os.path.isfile(fpath):
            return fpath
        raise ValueError(f"Path {fpath} is not a file!")
