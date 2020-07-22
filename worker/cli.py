#!/usr/bin/env python

import copy
import os
import struct

import click

from . import terraform as tf
from . import vault
from .main import State, create_table, get_aws_id

DEFAULT_CONFIG = "{}/worker.yaml".format(os.getcwd())
DEFAULT_REPOSITORY_PATH = "{}".format(os.getcwd())
DEFAULT_S3_BUCKET = "launchpad-terraform-states"
DEFAULT_S3_PREFIX = "terraform/state/{deployment}"
DEFAULT_AWS_REGION = "us-west-2"
DEFAULT_STATE_REGION = "us-west-2"
DEFAULT_TERRFORM = "/usr/local/bin/terraform"


def validate_deployment(ctx, deployment, name):
    """Validate the deployment is an 8 char name."""
    if len(name) > 16:
        click.secho("deployment must be less than 16 characters", fg="red")
        raise SystemExit(2)
    return name


def validate_host():
    """Ensure that the script is being run on a supported platform."""
    if struct.calcsize("P") * 8 != 64:
        click.secho("worker can only be run on 64 bit hosts, in 64 bit mode", fg="red")
        raise SystemExit(2)
    return True


def validate_keypair(pubkey, privkey, deployment, temp_dir, args):
    """Validate the provided SSH key values, and their existence in vault."""
    if pubkey is not None and privkey is None:
        click.secho("must pass --ssh-private-key when you supply a public SSH key")
        raise SystemExit(2)

    if pubkey is None and privkey is not None:
        click.secho("must pass --ssh-public-key when you supply a private SSH key")
        raise SystemExit(2)

    if pubkey is None and privkey is None:
        # No keys were passed so check inside of vault
        if not vault.check_keys(args.vault_address, args.vault_token, deployment):
            # No keys in vault, so generate a pair and save them
            pubkey, privkey = generate_keypair(temp_dir, deployment)
            vault.store_keys(
                args.vault_address, args.vault_token, deployment, pubkey, privkey
            )
    else:
        # Keys were passed on the command line, overwrite what is in vault
        vault.store_keys(
            args.vault_address, args.vault_token, deployment, pubkey, privkey
        )


@click.group()
@click.option(
    "--aws-access-key-id",
    required=True,
    envvar="AWS_ACCESS_KEY_ID",
    help="AWS Access key",
)
@click.option(
    "--aws-secret-access-key",
    required=True,
    prompt=True,
    hide_input=True,
    envvar="AWS_SECRET_ACCESS_KEY",
    help="AWS access key secret",
)
@click.option(
    "--aws-session-token",
    required=False,
    prompt=True,
    hide_input=True,
    envvar="AWS_SESSION_TOKEN",
    help="AWS access key token",
    default=""
)
@click.option(
    "--aws-region",
    envvar="AWS_DEFAULT_REGION",
    default=DEFAULT_AWS_REGION,
    help="AWS Region to build in",
)
@click.option(
    "--state-region",
    default=DEFAULT_STATE_REGION,
    help="AWS region where terraform state bucket exists",
)
@click.option(
    "--config-file", default=DEFAULT_CONFIG, envvar="WORKER_CONFIG_FILE", required=True
)
@click.option(
    "--repository-path",
    default=DEFAULT_REPOSITORY_PATH,
    envvar="WORKER_REPOSITORY_PATH",
    required=True,
    help="The path to the k8s-infra repository",
)
@click.pass_context
def cli(context, **kwargs):
    """CLI for the worker utility."""
    validate_host()
    config_file = kwargs["config_file"]
    try:
        context.obj = State(args=kwargs)
    except FileNotFoundError:
        click.secho(
            "configuration file {} not found".format(config_file), fg="red", err=True
        )
        raise SystemExit(1)


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
    "--s3-bucket",
    default=DEFAULT_S3_BUCKET,
    help="The s3 bucket for storing terraform state",
)
@click.option(
    "--s3-prefix",
    default=DEFAULT_S3_PREFIX,
    help="The prefix in the bucket for the definitions to use",
)
@click.option(
    "--terraform-bin",
    default=DEFAULT_TERRFORM,
    help="The complate location of the terraform binary",
)
@click.option("--limit", help="limit operations to a single definition", multiple=True)
@click.argument("deployment", callback=validate_deployment)
@click.pass_obj
def terraform(
    obj,
    clean,
    tf_apply,
    destroy,
    show_output,
    s3_bucket,
    s3_prefix,
    terraform_bin,
    limit,
    deployment,
):  # noqa: E501
    """Build a deployment."""
    if tf_apply and destroy:
        click.secho("can not apply and destroy at the same time", fg="red")
        raise SystemExit(1)
    plan_for = "apply"

    # If the default value is used, render the deployment name into it
    if s3_prefix == DEFAULT_S3_PREFIX:
        s3_prefix = DEFAULT_S3_PREFIX.format(deployment=deployment)
    obj.clean = clean
    obj.add_arg("s3_bucket", s3_bucket)
    obj.add_arg("s3_prefix", s3_prefix)
    obj.add_arg("terraform_bin", terraform_bin)
    obj.add_arg(
        "aws_account_id",
        get_aws_id(obj.args.aws_access_key_id, obj.args.aws_secret_access_key, obj.args.aws_session_token),
    )

    click.secho("loading config file {}".format(obj.args.config_file), fg="green")
    obj.load_config(obj.args.config_file)

    click.secho("building deployment {}".format(deployment), fg="green")
    click.secho("using temporary Directory:{}".format(obj.temp_dir), fg="yellow")

    # common setup required for all definitions
    click.secho("downloading plugins", fg="green")
    tf.download_plugins(obj.config["terraform"]["plugins"], obj.temp_dir)
    tf.prep_modules(obj.args.repository_path, obj.temp_dir)
    create_table(
        "terraform-{}".format(deployment),
        obj.args.state_region,
        obj.args.aws_access_key_id,
        obj.args.aws_secret_access_key,
        obj.args.aws_session_token)

    # update mechanism for definitions
    # first determine apply/destroy
    # fix order
    # plan for apply/destroy
    # only execute apply/destroy if plan succeeds
    # fail / exit on any error

    tf_items = []

    # setup tf_items to capture the limit/order based on options
    if destroy:
        for name, body in reversed(obj.config["terraform"]["definitions"].items()):
            if limit and name not in limit:
                continue
            tf_items.append((name, body))
        plan_for = "destroy"
    else:
        for name, body in obj.config["terraform"]["definitions"].items():
            if limit and name not in limit:
                continue
            tf_items.append((name, body))

    for name, body in tf_items:
        execute = False
        # copy definition files / templates etc.
        click.secho("preparing definition: {}".format(name), fg="green")
        tf.prep_def(
            name,
            body,
            obj.config["terraform"],
            obj.temp_dir,
            obj.args.repository_path,
            deployment,
            obj.args,
        )

        # run terraform init
        try:
            tf.run(
                name,
                obj.temp_dir,
                terraform_bin,
                "init",
                obj.args.aws_access_key_id,
                obj.args.aws_secret_access_key,
                obj.args.aws_session_token,
                debug=show_output,
            )
        except tf.TerraformError:
            click.secho("error running terraform init", fg="red")
            raise SystemExit(1)

        click.secho("planning definition: {}".format(name), fg="green")

        # run terraform plan
        try:
            tf.run(
                name,
                obj.temp_dir,
                terraform_bin,
                "plan",
                obj.args.aws_access_key_id,
                obj.args.aws_secret_access_key,
                obj.args.aws_session_token,
                debug=show_output,
                plan_action=plan_for,
            )
        except tf.PlanChange:
            execute = True
        except tf.TerraformError:
            click.secho(
                "error planning terraform definition: {}!".format(name), fg="red"
            )
            raise SystemExit(1)

        if execute and tf_apply:
            click.secho("plan changes for {}, applying".format(name), fg="yellow")
        elif execute and destroy:
            click.secho("plan changes for {}, destroying".format(name), fg="yellow")
        elif not execute:
            click.secho("no plan changes for {}".format(name), fg="yellow")
            continue

        try:
            tf.run(
                name,
                obj.temp_dir,
                terraform_bin,
                plan_for,
                obj.args.aws_access_key_id,
                obj.args.aws_secret_access_key,
                obj.args.aws_session_token,
                debug=show_output,
            )
        except tf.TerraformError:
            click.secho(
                "error with terraform {} on definition {}, exiting".format(
                    plan_for, name
                ),
                fg="red",
            )
            raise SystemExit(1)
        else:
            click.secho(
                "terraform {} complete for {}".format(plan_for, name), fg="green"
            )
