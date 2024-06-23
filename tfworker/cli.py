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
from typing import Any, Dict, Union

import click
from pydantic import ValidationError

import tfworker.util.log as log
from tfworker.commands.clean import CleanCommand
from tfworker.commands.env import EnvCommand
from tfworker.commands.root import RootCommand
from tfworker.commands.terraform import TerraformCommand
from tfworker.types.app_state import AppState
from tfworker.types.cli_options import (
    CLIOptionsClean,
    CLIOptionsRoot,
    CLIOptionsTerraform,
)
from tfworker.util.cli import (
    handle_option_error,
    pydantic_to_click,
    validate_deployment,
    validate_host,
)


class CSVType(click.types.StringParamType):
    name = "csv"
    envvar_list_splitter = ","

    def __repr__(self):
        return "CSV"


@click.group()
@pydantic_to_click(CLIOptionsRoot)
@click.version_option(package_name="terraform-worker")
@click.pass_context
def cli(ctx: click.Context, **kwargs):
    """CLI for the worker utility."""
    try:
        validate_host()
    except NotImplementedError as e:
        log.msg(str(e), log.LogLevel.ERROR)
        ctx.exit(1)

    try:
        options = CLIOptionsRoot.model_validate(kwargs)
    except ValidationError as e:
        handle_option_error(e, ctx)

    log.log_level = log.LogLevel[options.log_level]
    log.msg(f"set log level to {options.log_level}", log.LogLevel.DEBUG)
    app_state=AppState(root_options=options)
    ctx.obj = app_state
    rc = RootCommand(ctx)
    ctx.obj.root_command = rc
    log.msg("finished intializing root command", log.LogLevel.TRACE)


@cli.command()
@pydantic_to_click(CLIOptionsClean)
@click.argument("deployment", callback=validate_deployment)
@click.pass_context
def clean(ctx: click.Context, **kwargs):  # noqa: E501
    """clean up terraform state"""
    # clean just items if limit supplied, or everything if no limit
    try:
        options = CLIOptionsClean.model_validate(kwargs)
    except ValidationError as e:
        handle_option_error(e, ctx)

    ctx.obj.clean_config = options
    log.info(f"cleaning deployment {kwargs.get('deployment')}")
    log.info(f"working in directory: {ctx.obj.root_config.working_dir}")
    log.info(f"limiting to: {options.limit}")
    # CleanCommand(rootc, *args, **kwargs).exec()


@cli.command()
@pydantic_to_click(CLIOptionsTerraform)
@click.argument("deployment", envvar="WORKER_DEPLOYMENT", callback=validate_deployment)
@click.pass_context
def terraform(ctx: click.Context, deployment: str, **kwargs):
    """execute terraform orchestration"""
    try:
        options = CLIOptionsTerraform.model_validate(kwargs)
    except ValidationError as e:
        handle_option_error(e, ctx)

    ctx.obj.terraform_options = options
    tfc = TerraformCommand(ctx, deployment=deployment)


    # make it through init refactoring first....
    # tfc.exec()

    # try:
    #     tfc = TerraformCommand(rootc, *args, **kwargs)
    # except FileNotFoundError as e:
    #     click.secho(f"terraform binary not found: {e.filename}", fg="red", err=True)
    #     raise SystemExit(1)

    # click.secho(f"building deployment {kwargs.get('deployment')}", fg="green")
    # click.secho(f"working in directory: {tfc.temp_dir}", fg="yellow")

    # tfc.exec()
    # sys.exit(0)


@cli.command()
@click.pass_obj
def env(rootc, *args, **kwargs):
    # provide environment variables from backend to configure shell environment
    env = EnvCommand(rootc, *args, **kwargs)
    env.exec()
    sys.exit(0)


if __name__ == "__main__":
    cli()
