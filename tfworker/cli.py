#!/usr/bin/env python
import sys
from typing import Any, Dict, Union

import click
from pydantic import ValidationError

import tfworker.util.log as log
from tfworker.commands.config import log_limiter

# from tfworker.commands.clean import CleanCommand
from tfworker.commands.env import EnvCommand
from tfworker.commands.root import RootCommand
from tfworker.commands.terraform import TerraformCommand
from tfworker.types import (
    AppState,
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
        handle_option_error(e)

    log.log_level = log.LogLevel[options.log_level]
    log.msg(f"set log level to {options.log_level}", log.LogLevel.DEBUG)
    app_state = AppState(root_options=options)
    ctx.obj = app_state
    RootCommand()
    log.trace("finished intializing root command")


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
        handle_option_error(e)

    ctx.obj.clean_options = options
    log.info(f"cleaning deployment {kwargs.get('deployment')}")
    log.info(f"working in directory: {ctx.obj.root_options.working_dir}")
    log_limiter()
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
        handle_option_error(e)

    ctx.obj.terraform_options = options
    log.info(f"building Deployment: {deployment}")
    log_limiter()
    tfc = TerraformCommand(deployment=deployment)

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
