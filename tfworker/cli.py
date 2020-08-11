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


import copy
import os
import struct

import click

from tfworker import terraform as tf
from tfworker.main import State, create_table, get_aws_id
from tfworker.providers.aws import aws_config, clean_bucket_state, clean_locking_state
from tfworker.providers import StateError

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
    default=DEFAULT_AWS_REGION,
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
    "--s3-bucket",
    default=DEFAULT_S3_BUCKET,
    help="The s3 bucket for storing terraform state",
)
@click.option(
    "--s3-prefix",
    default=DEFAULT_S3_PREFIX,
    help="The prefix in the bucket for the definitions to use",
)
@click.option("--limit", help="limit operations to a single definition", multiple=True)
@click.argument("deployment", callback=validate_deployment)
@click.pass_obj
def clean(
    obj, s3_bucket, s3_prefix, limit, deployment,
):  # noqa: E501
    """ clean up terraform state """
    if s3_prefix == DEFAULT_S3_PREFIX:
        s3_prefix = DEFAULT_S3_PREFIX.format(deployment=deployment)

    obj.clean = clean
    obj.add_arg("s3_bucket", s3_bucket)
    obj.add_arg("s3_prefix", s3_prefix)
    config = get_aws_config(obj, deployment)

    # clean just items if limit supplied, or everything if no limit
    if len(limit) > 0:
        for limit_item in limit:
            click.secho(
                "when using limit, dynamodb tables won't be completely dropped",
                fg="yellow",
            )
            try:
                # the bucket state deployment is part of the s3 prefix
                clean_bucket_state(config, definition=limit_item)
                # deployment name needs specified to determine the dynamo table
                clean_locking_state(config, deployment, definition=limit_item)
            except StateError as e:
                click.secho("error deleting state: {}".format(e), fg="red")
                raise SystemExit(1)
    else:
        try:
            clean_bucket_state(config)
        except StateError as e:
            click.secho("error deleting state: {}".format(e))
            raise SystemExit(1)
        clean_locking_state(config, deployment)


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

    # configuration for AWS interactions
    config = get_aws_config(obj, deployment)

    obj.add_arg(
        "aws_account_id",
        get_aws_id(config.key_id, config.key_secret, config.session_token),
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
        config.state_region,
        config.key_id,
        config.key_secret,
        config.session_token,
    )

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
                config.key_id,
                config.key_secret,
                key_token=config.session_token,
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
                config.key_id,
                config.key_secret,
                key_token=config.session_token,
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
                config.key_id,
                config.key_secret,
                key_token=config.session_token,
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


def get_aws_config(obj, deployment):
    """ returns an aws_config based on the paramenters sent to CLI """
    # build params for aws_config based on inputs

    config_args = dict()
    if obj.args.aws_access_key_id is not None:
        config_args["key_id"] = obj.args.aws_access_key_id

    if obj.args.aws_secret_access_key is not None:
        config_args["key_secret"] = obj.args.aws_secret_access_key

    if obj.args.aws_session_token is not None:
        config_args["session_token"] = obj.args.aws_session_token

    if obj.args.aws_profile is not None:
        config_args["aws_profile"] = obj.args.aws_profile

    if obj.args.aws_role_arn is not None:
        config_args["role_arn"] = obj.args.aws_role_arn

    config = aws_config(
        obj.args.aws_region,
        obj.args.state_region,
        deployment,
        obj.args.s3_bucket,
        obj.args.s3_prefix,
        **config_args
    )
    return config
