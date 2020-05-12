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

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization as crypto_serialization
from cryptography.hazmat.backends import default_backend as crypto_default_backend


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
        with open(config_file, "r") as cfile:
            self.config = ordered_config_load(cfile, self.args)

    class StateArgs(object):
        """A class to hold arguments in the state for easier access."""

        pass


def generate_keypair(tempdir, name):
    """Generate an ssh keypair, and write to the tempdir."""
    key = rsa.generate_private_key(
        backend=crypto_default_backend(), public_exponent=65537, key_size=4096
    )

    private_key = key.private_bytes(
        crypto_serialization.Encoding.PEM,
        crypto_serialization.PrivateFormat.PKCS8,
        crypto_serialization.NoEncryption(),
    ).decode()

    public_key = (
        key.public_key()
        .public_bytes(
            crypto_serialization.Encoding.OpenSSH,
            crypto_serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )

    with open("{}/{}".format(tempdir, name), mode="w") as priv_key:
        priv_key.write(private_key)
        os.chmod("{}/{}".format(tempdir, name), 0o600)

    with open("{}/{}.pub".format(tempdir, name), mode="w") as pub_key:
        pub_key.write(public_key)

    return ("{}/{}.pub".format(tempdir, name), "{}/{}".format(tempdir, name))


def create_table(name, region, key_id, key_secret, read_capacity=1, write_capacity=1):
    """Create a dynamodb table."""
    client = boto3.client(
        "dynamodb",
        region_name=region,
        aws_access_key_id=key_id,
        aws_secret_access_key=key_secret,
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


def get_aws_id(key_id, key_secret):
    """Return the AWS account ID."""
    client = boto3.client(
        "sts", aws_access_key_id=key_id, aws_secret_access_key=key_secret
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
