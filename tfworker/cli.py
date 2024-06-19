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


import sys

import click
from pydantic import ValidationError

import tfworker.types as tf_types
from tfworker.commands import (
    CleanCommand,
    EnvCommand,
    RootCommand,
    TerraformCommand,
    VersionCommand,
)
from tfworker.util.cli import pydantic_to_click
from tfworker.util.system import get_platform


def validate_deployment(ctx, deployment, name):
    """Validate the deployment is no more than 32 characters."""
    if len(name) > 32:
        click.secho("deployment must be less than 32 characters", fg="red")
        raise SystemExit(1)
    if " " in name:
        click.secho("deployment must not contain spaces", fg="red")
        raise SystemExit(1)
    return name


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


class CSVType(click.types.StringParamType):
    name = "csv"
    envvar_list_splitter = ","

    def __repr__(self):
        return "CSV"


@click.group()
@pydantic_to_click(tf_types.CLIOptionsRoot)
@click.pass_context
def cli(ctx, **kwargs):
    """CLI for the worker utility."""
    try:
        options = tf_types.CLIOptionsRoot(**kwargs)
        validate_host()
        ctx.obj = RootCommand(options)
    except ValidationError as e:
        click.echo(f"Error in options: {e}")
        ctx.exit(1)


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
