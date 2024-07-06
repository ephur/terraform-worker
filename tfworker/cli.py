#!/usr/bin/env python
import click
from pydantic import ValidationError

import tfworker.util.log as log
from tfworker.app_state import AppState
from tfworker.cli_options import CLIOptionsClean, CLIOptionsRoot, CLIOptionsTerraform
from tfworker.commands.clean import CleanCommand
from tfworker.commands.config import log_limiter
from tfworker.commands.env import EnvCommand
from tfworker.commands.root import RootCommand
from tfworker.commands.terraform import TerraformCommand
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
    """
    The terraform worker is a command line utility to orchestrate terraform

    The goal is to reduce the amount of boiler plate terraform code, and allow
    for both a more dynamic execution, as well as a more controlled execution.
    Through a combinatin of definitions and providers specified in the configuration
    file, the worker can be used to build, destroy, and manage terraform deployments.
    """
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
    register_plugins()
    RootCommand()
    log.trace("finished intializing root command")


@cli.command()
@pydantic_to_click(CLIOptionsClean)
@click.argument("deployment", envvar="WORKER_DEPLOYMENT", callback=validate_deployment)
@click.pass_context
def clean(ctx: click.Context, deployment: str, **kwargs):  # noqa: E501
    """
    Clean up remnants of a deployment

    Once a deployment is destroyed via terraform, there are traces left in
    the backend such as S3 buckets, DynamoDB tables, etc. This command will
    verify the state is empty, and then remove those traces from the backend.
    """
    try:
        options = CLIOptionsClean.model_validate(kwargs)
    except ValidationError as e:
        handle_option_error(e)

    ctx.obj.clean_options = options
    log.info(f"cleaning Deployment: {deployment}")
    log_limiter()

    cc = CleanCommand(deployment=deployment)
    cc.exec()


@cli.command()
@pydantic_to_click(CLIOptionsTerraform)
@click.argument("deployment", envvar="WORKER_DEPLOYMENT", callback=validate_deployment)
@click.pass_context
def terraform(ctx: click.Context, deployment: str, **kwargs):
    """
    Execute terraform orchestration on all or a subset of definitions in a deployment

    The terraform command is used to plan, apply, and destroy terraform deployments. It
    dynamically creates and breaks down large states into smaller subsets of deployments
    which can share common parameters and a fixed set of providers.
    """
    # @TODO: Add support for a --target flag to target specific IDs in a definition

    try:
        options = CLIOptionsTerraform.model_validate(kwargs)
    except ValidationError as e:
        handle_option_error(e)

    ctx.obj.terraform_options = options
    log.info(f"building Deployment: {deployment}")
    log_limiter()
    tfc = TerraformCommand(deployment=deployment)

    # Prepare the provider cache
    tfc.prep_providers()
    # @TODO: Determine how much of this should be executed here, versus
    # orchestrated in the TerraformCommand classes .exec method
    tfc.terraform_init()
    tfc.terraform_plan()
    tfc.terraform_apply_or_destroy()


@cli.command()
@click.pass_context
def env(ctx: click.Context, **kwargs):
    """
    Export environment variables for the configured backend

    This command can be useful to setup environment credentials, that the
    worker will use. It handles configuration for the different backends
    allowing you to `eval` the output to have terraform commands work as
    the worker will execute them. This can be helpful when doing manual
    state management
    """
    env = EnvCommand()
    env.exec()


# @TODO: Command to list all definitions in the backend for a given deployment
# @TODO: Command to pull the remote state for a given deployment


def register_plugins():
    """
    Register the plugins
    """

    # Register Handlers
    log.trace("registering handlers")
    import tfworker.handlers  # noqa: F401

    # from tfworker.handlers.bitbucket import BitbucketHandler  # noqa: F401
    # from tfworker.handlers.s3 import S3Handler  # noqa: F401
    # from tfworker.handlers.trivy import TrivyHandler  # noqa: F401
    # Register Copiers
    log.trace("registering copiers")
    import tfworker.copier  # noqa: F401


if __name__ == "__main__":
    cli()
