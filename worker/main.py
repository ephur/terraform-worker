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
import re
import shlex
import shutil
import subprocess
import tempfile
from collections import OrderedDict

import boto3
import click
import yaml


class State(object):
    """A module to hold the state of the application."""

    def __init__(self, args=None, config_file=None, clean=True):
        """Setup state with args that are passed."""
        self.clean = clean
        self.temp_dir = tempfile.mkdtemp()
        self.args = self.StateArgs()

        if args is not None:
            self.add_args(args)

        if config_file is not None:
            self.load_config(config_file)

    def __del__(self):
        """Cleanup the temporary directory after execution."""
        if self.clean:
            shutil.rmtree(self.temp_dir)

    def add_args(self, args):
        """Add a dictionary of args."""
        for k, v in args.items():
            self.add_arg(k, v)

    def add_arg(self, k, v):
        """Add an argument to the state args."""
        setattr(self.args, k, v)
        return None

    def load_config(self, config_file):
        try:
            with open(config_file, "r") as cfile:
                self.config = ordered_config_load(cfile, self.args)
        except IOError:
            click.secho(
                "Unable to open configuration file: {}".format(config_file), fg="red"
            )
            raise SystemExit(1)

    class StateArgs(object):
        """A class to hold arguments in the state for easier access."""

        pass


def create_table(
    name, region, key_id, key_secret, session_token, read_capacity=1, write_capacity=1
):
    """Create a dynamodb table."""
    client = boto3.client(
        "dynamodb",
        region_name=region,
        aws_access_key_id=key_id,
        aws_secret_access_key=key_secret,
        aws_session_token=session_token,
    )
    tables = client.list_tables()
    table_key = "LockID"
    if name in tables["TableNames"]:
        click.secho("DynamoDB lock table found, continuing.", fg="yellow")
    else:
        click.secho(
            "DynamoDB lock table not found, creating, please wait...", fg="yellow"
        )
        client.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": table_key, "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": table_key, "AttributeType": "S"},],
            ProvisionedThroughput={
                "ReadCapacityUnits": read_capacity,
                "WriteCapacityUnits": write_capacity,
            },
        )

        client.get_waiter("table_exists").wait(
            TableName=name, WaiterConfig={"Delay": 10, "MaxAttempts": 30}
        )


def get_aws_id(key_id, key_secret, session_token=None):
    """Return the AWS account ID."""
    client = boto3.client(
        "sts",
        aws_access_key_id=key_id,
        aws_secret_access_key=key_secret,
        aws_session_token=session_token,
    )
    return client.get_caller_identity()["Account"]


def ordered_config_load(
    stream, args, Loader=yaml.SafeLoader, object_pairs_hook=OrderedDict
):
    """
    Load a yaml config, and replace templated items.

    Derived from:
    https://stackoverflow.com/questions/5121931/in-python-how-can-you-load-yaml-mappings-as-ordereddicts
    """

    class OrderedLoader(Loader):
        pass

    def construct_mapping(loader, node):
        for item in node.value:
            if isinstance(item, tuple):
                for oneitem in item:
                    if isinstance(oneitem, yaml.ScalarNode):
                        oneitem.value = replace_vars(oneitem.value, args)

        loader.flatten_mapping(node)
        return object_pairs_hook(loader.construct_pairs(node))

    OrderedLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
    )
    return yaml.load(stream, OrderedLoader)


def replace_vars(var, args):
    """Replace variables with template values."""
    var_pattern = r"//\s*(\S*)\s*//"
    match = re.match(var_pattern, var, flags=0)
    if not match:
        return var
    try:
        var = getattr(args, match.group(1).replace("-", "_"))
    except AttributeError:
        raise (ValueError("substitution not found for {}".format(var)))
    return var


def pipe_exec(args, stdin=None, cwd=None, env=None):
    """
    A function to accept a list of commands and pipe them together.

    Takes optional stdin to give to the first item in the pipe chain.
    """
    count = 0
    commands = []
    if env is None:
        env = os.environ.copy()

    if not isinstance(args, list):
        args = [args]

    for i in args:
        if count == 0:
            if stdin is None:
                commands.append(
                    subprocess.Popen(
                        shlex.split(i),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        cwd=cwd,
                        env=env,
                    )
                )
            else:
                commands.append(
                    subprocess.Popen(
                        shlex.split(i),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        stdin=subprocess.PIPE,
                        cwd=cwd,
                        env=env,
                    )
                )
        else:
            commands.append(
                subprocess.Popen(
                    shlex.split(i),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=commands[count - 1].stdout,
                    cwd=cwd,
                    env=env,
                )
            )
        count = count + 1

    if stdin is not None:
        stdin_bytes = stdin.encode()
        if len(commands) > 1:
            commands[0].communicate(input=stdin_bytes)
            stdout, stderr = commands[-1].communicate()
            commands[-1].wait()
            returncode = commands[-1].returncode
        else:
            stdout, stderr = commands[0].communicate(input=stdin_bytes)
            commands[0].wait()
            returncode = commands[0].returncode
    else:
        stdout, stderr = commands[-1].communicate()
        commands[-1].wait()
        returncode = commands[-1].returncode

    return (returncode, stdout, stderr)
