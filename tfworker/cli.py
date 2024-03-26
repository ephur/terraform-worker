#!/usr/bin/env python
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


import os
import struct
import sys
from pathlib import Path

import click

from tfworker import constants as const
from tfworker.commands import CleanCommand, RootCommand, TerraformCommand
from tfworker.commands.env import EnvCommand
from tfworker.commands.root import get_platform
from tfworker.commands.version import VersionCommand


def validate_deployment(ctx, deployment, name):
    """Validate the deployment is no more than 32 characters."""
    if len(name) > 32:
        click.secho("deployment must be less than 32 characters", fg="red")
        raise SystemExit(1)
    if " " in name:
        click.secho("deployment must not contain spaces", fg="red")
        raise SystemExit(1)
    return name


def validate_gcp_creds_path(ctx, path, value):
    if value:
        if not os.path.isabs(value):
            value = os.path.abspath(value)
        if os.path.isfile(value):
            return value
        click.secho(f"Could not resolve GCP credentials path: {value}", fg="red")
        raise SystemExit(1)


def validate_host():
    """Ensure that the script is being run on a supported platform."""
    supported_opsys = ["darwin", "linux"]
    supported_machine = ["amd64", "arm64"]

    opsys, machine = get_platform()

    if opsys not in supported_opsys:
        click.secho(
            f"running on {opsys} is not supported",
            fg="red",
        )
        raise SystemExit(1)

    if machine not in supported_machine:
        click.secho(
            f"running on {machine} machines is not supported",
            fg="red",
        )
        raise SystemExit(1)

    return True


def validate_working_dir(fpath):
    # if fpath is none, then a custom working directory was not defined, so this validates
    if fpath is None:
        return
    with Path(fpath) as wpath:
        if not wpath.exists():
            click.secho(f"Working path {fpath} does not exist!", fg="red")
            raise SystemExit(1)
        if not wpath.is_dir():
            click.secho(f"Working path {fpath} is not a directory!", fg="red")
            raise SystemExit(1)
        if any(wpath.iterdir()):
            click.secho(f"Working path {fpath} must be empty!", fg="red")
            raise SystemExit(1)


class CSVType(click.types.StringParamType):
    name = "csv"
    envvar_list_splitter = ","

    def __repr__(self):
        return "CSV"


@click.group()
@click.option(
    "--aws-access-key-id",
    envvar="AWS_ACCESS_KEY_ID",
    help="AWS Access key",
)
@click.option(
    "--aws-secret-access-key",
    envvar="AWS_SECRET_ACCESS_KEY",
    help="AWS access key secret",
)
@click.option(
    "--aws-session-token",
    envvar="AWS_SESSION_TOKEN",
    help="AWS access key token",
)
@click.option(
    "--aws-role-arn",
    envvar="AWS_ROLE_ARN",
    help="If provided, credentials will be used to assume this role (complete ARN)",
)
@click.option(
    "--aws-external-id",
    envvar="AWS_EXTERNAL_ID",
    help="If provided, will be used to assume the role specified by --aws-role-arn",
)
@click.option(
    "--aws-region",
    envvar="AWS_DEFAULT_REGION",
    default=const.DEFAULT_AWS_REGION,
    help="AWS Region to build in",
)
@click.option(
    "--aws-profile",
    envvar="AWS_PROFILE",
    help="The AWS/Boto3 profile to use",
)
@click.option(
    "--gcp-region",
    envvar="GCP_REGION",
    default=const.DEFAULT_GCP_REGION,
    help="Region to build in",
)
@click.option(
    "--gcp-creds-path",
    envvar="GCP_CREDS_PATH",
    help=(
        "Relative path to the credentials JSON file for the service account to be used."
    ),
    callback=validate_gcp_creds_path,
)
@click.option(
    "--gcp-project",
    envvar="GCP_PROJECT",
    help="GCP project name to which work will be applied",
)
@click.option(
    "--config-file",
    default=const.DEFAULT_CONFIG,
    envvar="WORKER_CONFIG_FILE",
    required=True,
)
@click.option(
    "--repository-path",
    default=const.DEFAULT_REPOSITORY_PATH,
    envvar="WORKER_REPOSITORY_PATH",
    required=True,
    help="The path to the terraform module repository",
)
@click.option(
    "--backend",
    type=click.Choice(["s3", "gcs"]),
    envvar="WORKER_BACKEND",
    help="State/locking provider. One of: s3, gcs",
)
@click.option(
    "--backend-bucket",
    envvar="WORKER_BACKEND_BUCKET",
    help="Bucket (must exist) where all terraform states are stored",
)
@click.option(
    "--backend-prefix",
    default=const.DEFAULT_BACKEND_PREFIX,
    envvar="WORKER_BACKEND_PREFIX",
    help=f"Prefix to use in backend storage bucket for all terraform states (DEFAULT: {const.DEFAULT_BACKEND_PREFIX})",
)
@click.option(
    "--backend-region",
    default=const.DEFAULT_AWS_REGION,
    help="Region where terraform rootc/lock bucket exists",
)
@click.option(
    "--backend-use-all-remotes/--no-backend-use-all-remotes",
    default=False,
    envvar="WORKER_BACKEND_USE_ALL_REMOTES",
    help="Generate remote data sources based on all definition paths present in the backend",
)
@click.option(
    "--create-backend-bucket/--no-create-backend-bucket",
    default=True,
    help="Create the backend bucket if it does not exist",
)
@click.option(
    "--config-var",
    multiple=True,
    default=[],
    help='key=value to be supplied as jinja variables in config_file under "var" dictionary, can be specified multiple times',
)
@click.option(
    "--working-dir",
    envvar="WORKER_WORKING_DIR",
    default=None,
    help="Specify the path to use instead of a temporary directory, must exist, be empty, and be writeable, --clean applies to this directory as well",
)
@click.option(
    "--clean/--no-clean",
    default=None,
    envvar="WORKER_CLEAN",
    help="clean up the temporary directory created by the worker after execution",
)
@click.option(
    "--backend-plans/--no-backend-plans",
    default=False,
    envvar="WORKER_BACKEND_PLANS",
    help="store plans in the backend",
)
@click.pass_context
def cli(context, **kwargs):
    """CLI for the worker utility."""
    validate_host()
    validate_working_dir(kwargs.get("working_dir", None))
    config_file = kwargs["config_file"]
    try:
        context.obj = RootCommand(args=kwargs)
    except FileNotFoundError:
        click.secho(f"configuration file {config_file} not found", fg="red", err=True)
        raise SystemExit(1)


@cli.command()
@click.option(
    "--limit",
    help="limit operations to a single definition",
    envvar="WORKER_LIMIT",
    multiple=True,
    type=CSVType(),
)
@click.argument("deployment", callback=validate_deployment)
@click.pass_obj
def clean(rootc, *args, **kwargs):  # noqa: E501
    """clean up terraform state"""
    # clean just items if limit supplied, or everything if no limit
    CleanCommand(rootc, *args, **kwargs).exec()


@cli.command()
def version():
    """display program version"""
    VersionCommand().exec()
    sys.exit(0)


@cli.command()
@click.option(
    "--plan-file-path",
    default=None,
    envvar="WORKER_PLAN_FILE_PATH",
    help="path to plan files, with plan it will save to this location, apply will read from it",
)
@click.option(
    "--apply/--no-apply",
    "tf_apply",
    envvar="WORKER_APPLY",
    default=False,
    help="apply the terraform configuration",
)
@click.option(
    "--plan/--no-plan",
    "tf_plan",
    envvar="WORKER_PLAN",
    type=bool,
    default=True,
    help="toggle running a plan, plan will still be skipped if using a saved plan file with apply",
)
@click.option(
    "--force/--no-force",
    "force",
    default=False,
    envvar="WORKER_FORCE",
    help="force apply/destroy without plan change",
)
@click.option(
    "--destroy/--no-destroy",
    default=False,
    envvar="WORKER_DESTROY",
    help="destroy a deployment instead of create it",
)
@click.option(
    "--show-output/--no-show-output",
    default=True,
    envvar="WORKER_SHOW_OUTPUT",
    help="show output from terraform commands",
)
@click.option(
    "--terraform-bin",
    envvar="WORKER_TERRAFORM_BIN",
    help="The complate location of the terraform binary",
)
@click.option(
    "--b64-encode-hook-values/--no--b64-encode-hook-values",
    "b64_encode",
    default=False,
    envvar="WORKER_B64_ENCODE_HOOK_VALUES",
    help=(
        "Terraform variables and outputs can be complex data structures, setting this"
        " open will base64 encode the values for use in hook scripts"
    ),
)
@click.option(
    "--terraform-modules-dir",
    envvar="WORKER_TERRAFORM_MODULES_DIR",
    default="",
    help=(
        "Absolute path to the directory where terraform modules will be stored."
        "If this is not set it will be relative to the repository path at ./terraform-modules"
    ),
)
@click.option(
    "--limit",
    help="limit operations to a single definition",
    envvar="WORKER_LIMIT",
    multiple=True,
    type=CSVType(),
)
@click.option(
    "--provider-cache",
    envvar="WORKER_PROVIDER_CACHE",
    default=None,
    help="if provided this directory will be used as a cache for provider plugins",
)
@click.option(
    "--stream-output/--no-stream-output",
    help="stream the output from terraform command",
    envvar="WORKER_STREAM_OUTPUT",
    default=True,
)
@click.option(
    "--color/--no-color",
    help="colorize the output from terraform command",
    envvar="WORKER_COLOR",
    default=False,
)
@click.argument("deployment", envvar="WORKER_DEPLOYMENT", callback=validate_deployment)
@click.pass_obj
def terraform(rootc, *args, **kwargs):
    """execute terraform orchestration"""
    try:
        tfc = TerraformCommand(rootc, *args, **kwargs)
    except FileNotFoundError as e:
        click.secho(f"terraform binary not found: {e.filename}", fg="red", err=True)
        raise SystemExit(1)

    click.secho(f"building deployment {kwargs.get('deployment')}", fg="green")
    click.secho(f"working in directory: {tfc.temp_dir}", fg="yellow")

    # common setup required for all definitions
    click.secho("preparing provider plugins", fg="green")
    tfc.plugins.download()
    click.secho("preparing modules", fg="green")
    tfc.prep_modules()

    tfc.exec()
    sys.exit(0)


@cli.command()
@click.pass_obj
def env(rootc, *args, **kwargs):
    # provide environment variables from backend to configure shell environment
    env = EnvCommand(rootc, *args, **kwargs)
    env.exec()
    sys.exit(0)


if __name__ == "__main__":
    cli()
