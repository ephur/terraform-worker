#!/usr/bin/env python
# Copyright 2020 Richard Maynard (richard.maynard@gmail.com)
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

import click
from tfworker import constants as const
from tfworker.commands import CleanCommand, RootCommand, TerraformCommand
from tfworker.commands.root import get_platform


def validate_deployment(ctx, deployment, name):
    """Validate the deployment is an 8 char name."""
    if len(name) > 16:
        click.secho("deployment must be less than 16 characters", fg="red")
        raise SystemExit(2)
    return name


def validate_gcp_creds_path(ctx, path, value):
    if value:
        if not os.path.isabs(value):
            value = os.path.abspath(value)
        if os.path.isfile(value):
            return value
        click.secho(f"Could not resolve GCP credentials path: {value}", fg="red")
        raise SystemExit(3)


def validate_host():
    """Ensure that the script is being run on a supported platform."""
    supported_opsys = ["darwin", "linux"]
    supported_machine = ["amd64"]

    opsys, machine = get_platform()

    if opsys not in supported_opsys:
        click.secho(
            f"this application is currently not known to support {opsys}",
            fg="red",
        )
        raise SystemExit(2)

    if machine not in supported_machine:
        click.secho(
            f"this application is currently not known to support running on {machine} machines",
            fg="red",
        )

    if struct.calcsize("P") * 8 != 64:
        click.secho(
            "this application can only be run on 64 bit hosts, in 64 bit mode", fg="red"
        )
        raise SystemExit(2)

    return True


@click.group()
@click.option(
    "--aws-access-key-id",
    required=False,
    envvar="AWS_ACCESS_KEY_ID",
    help="AWS Access key",
    default=None,
)
@click.option(
    "--aws-secret-access-key",
    required=False,
    envvar="AWS_SECRET_ACCESS_KEY",
    help="AWS access key secret",
    default=None,
)
@click.option(
    "--aws-session-token",
    required=False,
    envvar="AWS_SESSION_TOKEN",
    help="AWS access key token",
    default=None,
)
@click.option(
    "--aws-role-arn",
    envvar="AWS_ROLE_ARN",
    help="If provided, credentials will be used to assume this role (complete ARN)",
    default=None,
    required=False,
)
@click.option(
    "--aws-region",
    envvar="AWS_DEFAULT_REGION",
    default=const.DEFAULT_AWS_REGION,
    help="AWS Region to build in",
)
@click.option(
    "--aws-profile",
    required=False,
    envvar="AWS_PROFILE",
    help="The AWS/Boto3 profile to use",
    default=None,
)
@click.option(
    "--gcp-region",
    envvar="GCP_REGION",
    default=const.DEFAULT_GCP_REGION,
    help="Region to build in",
)
@click.option(
    "--gcp-creds-path",
    required=False,
    envvar="GCP_CREDS_PATH",
    help=(
        "Relative path to the credentials JSON file for the service account to be used."
    ),
    default=None,
    callback=validate_gcp_creds_path,
)
@click.option(
    "--gcp-project",
    envvar="GCP_PROJECT",
    help="GCP project name to which work will be applied",
    default=None,
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
    help="State/locking provider. One of: s3, gcs",
)
@click.option(
    "--backend-bucket",
    default=const.DEFAULT_BACKEND_BUCKET,
    help="Region where terraform rootc/lock bucket exists",
)
@click.option(
    "--backend-prefix",
    default=const.DEFAULT_BACKEND_PREFIX,
    help="Region where terraform rootc/lock bucket exists",
)
@click.option(
    "--backend-region",
    default=const.DEFAULT_BACKEND_REGION,
    help="Region where terraform rootc/lock bucket exists",
)
@click.option(
    "--config-var",
    multiple=True,
    default=[],
    help='key=value to be supplied as jinja variables in config_file under "var" dictionary, can be specified multiple times',
)
@click.pass_context
def cli(context, **kwargs):
    """CLI for the worker utility."""
    validate_host()
    config_file = kwargs["config_file"]
    try:
        context.obj = RootCommand(args=kwargs)
    except FileNotFoundError:
        click.secho(f"configuration file {config_file} not found", fg="red", err=True)
        raise SystemExit(1)


@cli.command()
@click.option("--limit", help="limit operations to a single definition", multiple=True)
@click.argument("deployment", callback=validate_deployment)
@click.pass_obj
def clean(rootc, *args, **kwargs):  # noqa: E501
    """ clean up terraform state """
    # clean just items if limit supplied, or everything if no limit
    CleanCommand(rootc, *args, **kwargs).exec()
    sys.exit(0)


@cli.command()
@click.option(
    "--clean/--no-clean",
    default=True,
    help="clean up the temporary directory created by the worker after execution",
)
@click.option(
    "--apply/--no-apply",
    "tf_apply",
    default=False,
    help="apply the terraform configuration",
)
@click.option(
    "--force-apply/--no-force-apply",
    "force_apply",
    default=False,
    help="force apply without plan change",
)
@click.option(
    "--destroy/--no-destroy",
    default=False,
    help="destroy a deployment instead of create it",
)
@click.option(
    "--show-output/--no-show-output",
    default=False,
    help="shot output from terraform commands",
)
@click.option(
    "--terraform-bin",
    default=const.DEFAULT_TERRFORM,
    help="The complate location of the terraform binary",
)
@click.option(
    "--b64-encode-hook-values/--no--b64-encode-hook-values",
    "b64_encode",
    default=False,
    help=(
        "Terraform variables and outputs can be complex data structures, setting this"
        " open will base64 encode the values for use in hook scripts"
    ),
)
@click.option("--limit", help="limit operations to a single definition", multiple=True)
@click.argument("deployment", callback=validate_deployment)
@click.pass_obj
def terraform(rootc, *args, **kwargs):
    """ No do nothing """
    tfc = TerraformCommand(rootc, *args, **kwargs)

    click.secho(f"building deployment {kwargs.get('deployment')}", fg="green")
    click.secho(f"using temporary Directory: {tfc.temp_dir}", fg="yellow")

    # common setup required for all definitions
    click.secho("downloading plugins", fg="green")
    tfc.plugins.download()
    click.secho("preparing modules", fg="green")
    tfc.prep_modules()

    tfc.exec()
    sys.exit(0)


if __name__ == "__main__":
    cli()
