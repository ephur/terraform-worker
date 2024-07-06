import os
import shutil
from typing import List, Optional, Union

import click
from pydantic import (
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails

import tfworker.util.log as log
from tfworker import constants as const
from tfworker.backends import Backends
from tfworker.types import FreezableBaseModel
from tfworker.util.terraform import get_terraform_version


class CLIOptionsRoot(FreezableBaseModel):
    """
    CLIOptionsRoot is a Pydantic model that represents the root options for the CLI.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

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
    backend: Backends = Field(
        Backends.S3.name.lower(),
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
        description="The root repository/working path, any relative paths will be resolved from here",
    )
    working_dir: Optional[str] = Field(
        None,
        json_schema_extra={"env": "WORKER_WORKING_DIR"},
        description="Specify the path to use instead of a temporary directory, must exist, be empty, and be writeable, --clean applies to this directory as well",
    )

    @field_validator("backend", mode="before")
    @classmethod
    def validate_backend(cls, backend: Union[Backends, str]) -> Backends:
        """Validate the backend type.

        Args:
            backend: The backend type.

        Returns:
            The validated backend type.

        Raises:
            ValueError: If the backend is not supported.
        """
        # convert the backend str to the corresponding enum
        if isinstance(backend, str):
            try:
                selected_backend = Backends(backend.lower())
            except ValueError:
                raise ValueError(f"Backend {backend} is not supported!")
            return selected_backend
        return backend

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

    @field_validator("backend_prefix")
    @classmethod
    def validate_backend_prefix(cls, prefix: str) -> str:
        """Mutate the backend prefix to ensure there are no leading or trailing slashes, or double slashes.

        Args:
            prefix (str): The backend prefix.

        Returns:
            The validated backend prefix.
        """
        if prefix.startswith("/"):
            prefix = prefix[1:]
        if prefix.endswith("/"):
            prefix = prefix[:-1]
        if "//" in prefix:
            prefix = prefix.replace("//", "/")

        return prefix

    @field_validator("repository_path")
    @classmethod
    def validate_repository_path(cls, fpath: str) -> str:
        return validate_existing_dir(fpath)

    @field_validator("working_dir")
    @classmethod
    def validate_working_dir(cls, fpath: Union[str, None]) -> Union[str, None]:
        return validate_existing_dir(fpath, empty=True)


class CLIOptionsClean(FreezableBaseModel):
    """
    CLIOptionsClean is a Pydantic model that represents the options for the clean command.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    limit: Optional[List[str]] = Field(
        [],
        description="limit operations to a single definition, multiple values allowed, or separate with commas",
        json_schema_extra={"short_arg": "l", "env": "WORKER_LIMIT"},
    )

    @model_validator(mode="before")
    def validate_limit(cls, values):
        return validate_limit(values)


class CLIOptionsTerraform(FreezableBaseModel):
    """
    CLIOptionsTerraform is a Pydantic model that represents the options for the terraform command.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

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
        json_schema_extra={"env": "WORKER_B64_ENCODE"},
        description="Base64 encode Terraform variables and outputs for use in hook scripts",
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
    backend_use_all_remotes: bool = Field(
        False,
        json_schema_extra={"env": "WORKER_BACKEND_USE_ALL_REMOTES"},
        description="Generate remote data sources based on all definition paths present in the backend",
    )

    @model_validator(mode="before")
    @classmethod
    def validate_apply_and_destroy(cls, values):
        errors = []
        if values.get("apply") and values.get("destroy"):
            errors.append(
                InitErrorDetails(
                    loc=("--apply", "--apply"),
                    input=(values.get("apply")),
                    ctx={"error": "apply and destroy cannot both be true"},
                    type="value_error",
                )
            )
            errors.append(
                InitErrorDetails(
                    loc=("--destroy", "--destroy"),
                    input=(values.get("destroy")),
                    ctx={"error": "apply and destroy cannot both be true"},
                    type="value_error",
                )
            )
        if errors:
            raise ValidationError.from_exception_data("apply_and_destroy", errors)
        return values

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
            config = click.get_current_context().obj.loaded_config
            if config is not None:
                fpath = config.worker_options.get("terraform_bin")
                log.trace(f"Using terraform binary from config: {fpath}")
        else:
            log.trace(f"Using terraform binary from CLI: {fpath}")
        if fpath is None:
            fpath = shutil.which("terraform")
            log.trace(f"Using terraform binary from PATH: {fpath}")
        if fpath is None:
            raise ValueError(
                "Terraform binary not found in PATH, specify in config or with --terraform-bin"
            )
        if not os.path.isabs(fpath):
            fpath = os.path.abspath(fpath)
        if not os.path.isfile(fpath):
            raise ValueError(f"Terraform binary {fpath} does not exist!")
        if not os.access(fpath, os.X_OK):
            raise ValueError(f"Terraform binary {fpath} is not executable!")
        get_terraform_version(fpath, validation=True)
        log.trace(f"Terraform binary path validated: {fpath}")
        return fpath

    @field_validator("provider_cache")
    @classmethod
    def validate_provider_cache(cls, fpath: Union[str, None]) -> Union[str, None]:
        return validate_existing_dir(fpath)

    @field_validator("plan_file_path")
    @classmethod
    def validate_plan_file_path(cls, fpath: Union[str, None]) -> Union[str, None]:
        return validate_existing_dir(fpath)

    @model_validator(mode="before")
    def validate_limit(cls, values):
        return validate_limit(values)


def validate_existing_dir(fpath: Union[str, None], empty=False) -> Union[str, None]:
    """
    validate_existing_dir is called by multiple validators, it ensures
    a writable directory exists at the provided path, and optionally that
    it is empty

    Args:
        fpath (str): The path to the directory
        empty (bool): If the directory must be empty

    Returns:
        str: The absolute path to the directory

    Raises:
        ValueError: If the directory does not exist, is not a directory, is not writeable, or is not empty
    """
    if fpath is None:
        return
    if not os.path.isabs(fpath):
        fpath = os.path.abspath(fpath)
    if not os.path.isdir(fpath):
        raise ValueError(f"path {fpath} does not exist!")
    if not os.access(fpath, os.W_OK):
        raise ValueError(f"Ppath {fpath} is not writeable!")
    if empty and any(os.listdir(fpath)):
        raise ValueError(f"path {fpath} must be empty!")
    return fpath


def validate_limit(values):
    """
    validate_limit is called by multiple CLIOptions models to validate the limit field
    """
    if values.get("limit") is None:
        return values

    new_items = []
    # accept comma separated values and convert to list, same as passing --limit item_one --limit item_two
    for item in values["limit"]:
        if "," in item:
            new_items.extend(item.split(","))
        else:
            new_items.append(item)

    values["limit"] = new_items

    errors = []
    config = click.get_current_context().obj.loaded_config
    if config is not None:
        for item in values["limit"]:
            if item not in config.definitions.keys():
                errors.append(
                    InitErrorDetails(
                        loc=("--limit", "--limit"),
                        input=item,
                        ctx={"error": f"definition {item} not found in config"},
                        type="value_error",
                    )
                )
    if errors:
        raise ValidationError.from_exception_data("invalid_limit", errors)
    return values
