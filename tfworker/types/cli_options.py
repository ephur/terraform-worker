import os
from pathlib import Path
import shutil
from typing import List, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

import tfworker.util.log as log
from tfworker import constants as const
from tfworker.util.terraform import get_terraform_version


class CLIOptionsRoot(BaseModel):
    """
    CLIOptionsRoot is a Pydantic model that represents the root options for the CLI.
    """

    aws_access_key_id: Optional[str] = Field(
        None,
        json_schema_extra={"env": "AWS_ACCESS_KEY_ID"},
        description="AWS Access key",
    )
    aws_external_id: Optional[str] = Field(
        None,
        json_schema_extra={"env": "AWS_EXTERNAL_ID"},
        description="If provided, will be used to assume the role specified by --aws-role-arn",
    )
    aws_profile: Optional[str] = Field(
        None,
        json_schema_extra={"env": "AWS_PROFILE"},
        description="The AWS/Boto3 profile to use",
    )
    aws_role_arn: Optional[str] = Field(
        None,
        json_schema_extra={"env": "AWS_ROLE_ARN"},
        description="If provided, credentials will be used to assume this role (complete ARN)",
    )
    aws_secret_access_key: Optional[str] = Field(
        None,
        json_schema_extra={"env": "AWS_SECRET_ACCESS_KEY"},
        description="AWS access key secret",
    )
    aws_session_token: Optional[str] = Field(
        None,
        json_schema_extra={"env": "AWS_SESSION_TOKEN"},
        description="AWS access key token",
    )
    aws_region: str = Field(
        const.DEFAULT_AWS_REGION,
        json_schema_extra={"env": "AWS_DEFAULT_REGION"},
        description="AWS Region to build in",
    )
    backend: Optional[str] = Field(
        "s3",
        json_schema_extra={"env": "WORKER_BACKEND"},
        description="State/locking provider. One of: s3, gcs",
    )
    backend_bucket: Optional[str] = Field(
        None,
        json_schema_extra={"env": "WORKER_BACKEND_BUCKET"},
        description="Bucket (must exist) where all terraform states are stored",
    )
    backend_plans: bool = Field(
        False,
        json_schema_extra={"env": "WORKER_BACKEND_PLANS"},
        description="Store plans in the backend",
    )
    backend_prefix: str = Field(
        const.DEFAULT_BACKEND_PREFIX,
        json_schema_extra={"env": "WORKER_BACKEND_PREFIX"},
        description="Prefix to use in backend storage bucket for all terraform states",
    )
    backend_region: str = Field(
        const.DEFAULT_AWS_REGION,
        description="Region where terraform root/lock bucket exists",
    )
    backend_use_all_remotes: bool = Field(
        False,
        json_schema_extra={"env": "WORKER_BACKEND_USE_ALL_REMOTES"},
        description="Generate remote data sources based on all definition paths present in the backend",
    )
    create_backend_bucket: bool = Field(
        True, description="Create the backend bucket if it does not exist"
    )
    config_file: str = Field(
        const.DEFAULT_CONFIG,
        json_schema_extra={"env": "WORKER_CONFIG_FILE"},
        description="Path to the configuration file",
    )
    config_var: Optional[List[str]] = Field(
        [],
        description='key=value to be supplied as jinja variables in config_file under "var" dictionary, can be specified multiple times',
    )
    log_level: str = Field(
        "INFO",
        description="The level to use for logging/output",
        json_schema_extra={"env": "LOG_LEVEL"},
    )
    gcp_region: str = Field(
        const.DEFAULT_GCP_REGION,
        json_schema_extra={"env": "GCP_REGION"},
        description="Region to build in",
    )
    gcp_creds_path: Optional[str] = Field(
        None,
        json_schema_extra={"env": "GCP_CREDS_PATH"},
        description="Relative path to the credentials JSON file for the service account to be used.",
    )
    gcp_project: Optional[str] = Field(
        None,
        json_schema_extra={"env": "GCP_PROJECT"},
        description="GCP project name to which work will be applied",
    )
    repository_path: str = Field(
        const.DEFAULT_REPOSITORY_PATH,
        json_schema_extra={"env": "WORKER_REPOSITORY_PATH"},
        description="The path to the terraform module repository",
    )
    working_dir: Optional[str] = Field(
        None,
        json_schema_extra={"env": "WORKER_WORKING_DIR"},
        description="Specify the path to use instead of a temporary directory, must exist, be empty, and be writeable, --clean applies to this directory as well",
    )

    @field_validator("backend")
    @classmethod
    def validate_backend(cls, backend: str) -> str:
        """Validate the backend type.

        Args:
            backend: The backend type.

        Returns:
            The validated backend type.

        Raises:
            ValueError: If the backend is not supported.
        """
        if backend.lower() not in const.SUPPORTED_BACKENDS.keys():
            raise ValueError(f"Backend {backend} is not supported!")
        return backend.lower()

    @field_validator("config_file")
    @classmethod
    def validate_config_file(cls, fpath: str) -> str:
        """Validates the config file exists, and is readable

        Args:
            fpath (str): The path to the config file

        Returns:
            str: The absolute path to the config file

        Raises:
            ValueError: If the file does not exist or is not readable
        """
        if not os.path.isabs(fpath):
            fpath = os.path.abspath(fpath)
        if not os.path.isfile(fpath):
            raise ValueError(f"Config file {fpath} does not exist!")
        if not os.access(fpath, os.R_OK):
            raise ValueError(f"Config file {fpath} is not readable!")
        return fpath

    @field_validator("gcp_creds_path")
    @classmethod
    def validate_gcp_creds_path(cls, fpath: Union[str, None]) -> Union[str, None]:
        """Validate the GCP credentials path.

        Args:
            fpath (str): Path to the GCP credentials file.

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

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, level: str) -> str:
        """Validate the log level.

        Args:
            level(str): The log level.

        Returns:
            The normalized/validated log level.

        Raises:
            ValueError: If the log level is invalid.
        """
        try:
            _ = log.LogLevel[level.upper()]
            return level.upper()
        except KeyError:
            raise ValueError("Invalid log level")

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


class CLIOptionsClean(BaseModel):
    limit: Optional[List[str]] = Field(
        [],
        description="limit operations to a single definition, multiple values allowed, or separate with commas",
        json_schema_extra={"short_arg": "l", "env": "WORKER_LIMIT"},
    )

    @model_validator(mode="before")
    def validate_limit(cls, values):
        if values.get("limit") is None:
            return values

        new_items = []
        for item in values["limit"]:
            if "," in item:
                new_items.extend(item.split(","))
            else:
                new_items.append(item)

        values["limit"] = new_items
        return values


# @click.option(
#     "--plan-file-path",
#     default=None,
#     envvar="WORKER_PLAN_FILE_PATH",
#     help="path to plan files, with plan it will save to this location, apply will read from it",
# )
# @click.option(
#     "--apply/--no-apply",
#     "tf_apply",
#     envvar="WORKER_APPLY",
#     default=False,
#     help="apply the terraform configuration",
# )
# @click.option(
#     "--plan/--no-plan",
#     "tf_plan",
#     envvar="WORKER_PLAN",
#     type=bool,
#     default=True,
#     help="toggle running a plan, plan will still be skipped if using a saved plan file with apply",
# )
# @click.option(
#     "--force/--no-force",
#     "force",
#     default=False,
#     envvar="WORKER_FORCE",
#     help="force apply/destroy without plan change",
# )
# @click.option(
#     "--destroy/--no-destroy",
#     default=False,
#     envvar="WORKER_DESTROY",
#     help="destroy a deployment instead of create it",
# )
# @click.option(
#     "--show-output/--no-show-output",
#     default=True,
#     envvar="WORKER_SHOW_OUTPUT",
#     help="show output from terraform commands",
# )
# @click.option(
#     "--terraform-bin",
#     envvar="WORKER_TERRAFORM_BIN",
#     help="The complate location of the terraform binary",
# )
# @click.option(
#     "--b64-encode-hook-values/--no--b64-encode-hook-values",
#     "b64_encode",
#     default=False,
#     envvar="WORKER_B64_ENCODE_HOOK_VALUES",
#     help=(
#         "Terraform variables and outputs can be complex data structures, setting this"
#         " open will base64 encode the values for use in hook scripts"
#     ),
# )
# @click.option(
#     "--terraform-modules-dir",
#     envvar="WORKER_TERRAFORM_MODULES_DIR",
#     default="",
#     help=(
#         "Absolute path to the directory where terraform modules will be stored."
#         "If this is not set it will be relative to the repository path at ./terraform-modules"
#     ),
# )
# @click.option(
#     "--limit",
#     help="limit operations to a single definition",
#     envvar="WORKER_LIMIT",
#     multiple=True,
#     type=CSVType(),
# )
# @click.option(
#     "--provider-cache",
#     envvar="WORKER_PROVIDER_CACHE",
#     default=None,
#     help="if provided this directory will be used as a cache for provider plugins",
# )
# @click.option(
#     "--stream-output/--no-stream-output",
#     help="stream the output from terraform command",
#     envvar="WORKER_STREAM_OUTPUT",
#     default=True,
# )
# @click.option(
#     "--color/--no-color",
#     help="colorize the output from terraform command",
#     envvar="WORKER_COLOR",
#     default=False,
# )
class CLIOptionsTerraform(BaseModel):
    """
    CLIOptionsTerraform is a Pydantic model that represents the options for the terraform command.
    """

    apply: bool = Field(
        False,
        json_schema_extra={"env": "WORKER_APPLY"},
        description="Apply the terraform configuration",
    )
    destroy: bool = Field(
        False,
        json_schema_extra={"env": "WORKER_DESTROY"},
        description="Destroy a deployment instead of create it",
    )
    plan: bool = Field(
        True,
        json_schema_extra={"env": "WORKER_PLAN"},
        description="Toggle running a plan, plan will still be skipped if using a saved plan file with apply",
    )
    force: bool = Field(
        False,
        json_schema_extra={"env": "WORKER_FORCE"},
        description="Force apply/destroy without plan change",
    )
    plan_file_path: Optional[str] = Field(
        None,
        json_schema_extra={"env": "WORKER_PLAN_FILE_PATH"},
        description="Path to plan files, with plan it will save to this location, apply will read from it",
    )
    show_output: bool = Field(
        True,
        json_schema_extra={"env": "WORKER_SHOW_OUTPUT"},
        description="Show output from terraform commands",
    )
    terraform_bin: Optional[str] = Field(
        None,
        json_schema_extra={"env": "WORKER_TERRAFORM_BIN"},
        description="The complete location of the terraform binary",
    )
    b64_encode: bool = Field(
        False,
        json_schema_extra={"env": "WORKER_B64_ENCODE_HOOK_VALUES"},
        description="Base64 encode Terraform variables and outputs for use in hook scripts",
    )
    terraform_modules_dir: str = Field(
        "",
        json_schema_extra={"env": "WORKER_TERRAFORM_MODULES_DIR"},
        description="Absolute path to the directory where terraform modules will be stored. If not set, it will be relative to the repository path at ./terraform-modules",
    )
    limit: Optional[List[str]] = Field(
        None,
        json_schema_extra={"env": "WORKER_LIMIT"},
        description="Limit operations to a single definition",
    )
    provider_cache: Optional[str] = Field(
        None,
        json_schema_extra={"env": "WORKER_PROVIDER_CACHE"},
        description="Directory to be used as a cache for provider plugins",
    )
    stream_output: bool = Field(
        True,
        json_schema_extra={"env": "WORKER_STREAM_OUTPUT"},
        description="Stream the output from terraform command",
    )
    color: bool = Field(
        False,
        json_schema_extra={"env": "WORKER_COLOR"},
        description="Colorize the output from terraform command",
    )

    @field_validator("terraform_bin")
    @classmethod
    def validate_terraform_bin(cls, fpath: Union[str, None]) -> Union[str, None]:
        """Validate the terraform binary path.

        Args:
            fpath: Path to the terraform binary.

        Returns:
            Path to the terraform binary.

        Raises:
            ValueError: If the path does not exist or is not a file.
        """
        if fpath is None:
            fpath = shutil.which("terraform")

        print("VALIDATION", fpath)
        if not os.path.isabs(fpath):
            fpath = os.path.abspath(fpath)
        if os.path.isfile(fpath):
            (major, minor) = get_terraform_version(fpath)
        return fpath

        raise ValueError(f"Path {fpath} is not a file!")