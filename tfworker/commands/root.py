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

import platform
import re
import shutil
import tempfile
from collections import OrderedDict

import click
import yaml


class RootCommand:
    def __init__(self, args=None, clean=True):
        """Setup state with args that are passed."""
        self.clean = clean
        self.temp_dir = tempfile.mkdtemp()
        self.args = self.StateArgs()

        # Config accessors
        self.tf = None
        self._pullup_keys()

        if args is not None:
            self.add_args(args)

        if args.get("config_file"):
            click.secho(f"loading config file {args.get('config_file')}", fg="green")
            self.load_config(args.get("config_file"))

    def __del__(self):
        """Cleanup the temporary directory after execution."""
        if self.clean:
            try:
                shutil.rmtree(self.temp_dir)
            except Exception:
                pass

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

                # A little arbitrary, but decorate the top two levels
                # directly on self object
                self.tf = self.config.get("terraform", OrderedDict())
                self._pullup_keys()

        except IOError:
            click.secho(f"Unable to open configuration file: {config_file}", fg="red")
            raise SystemExit(1)

    def _pullup_keys(self):
        """_pullup_keys is a utility function to place keys from the loaded config file
        directly on the RootCommand instance."""
        for k in [
            "definitions",
            "plugins",
            "providers",
            "remote_vars",
            "template_vars",
            "terraform_vars",
        ]:
            if self.tf:
                setattr(self, f"{k}_odict", self.tf.get(k, OrderedDict()))
            else:
                setattr(self, f"{k}_odict", None)

    class StateArgs:
        """A class to hold arguments in the state for easier access."""

        pass


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
        raise (ValueError(f"substitution not found for {var}"))
    return var


def get_platform():
    """
    get_platform will return a formatted operating system / architecture
    tuple that is consistent with common distribution creation tools
    """

    # strip off "2" which only appears on old linux kernels
    opsys = platform.system().rstrip("2").lower()

    # make sure machine uses consistent format
    machine = platform.machine()
    if machine == "x86_64":
        machine = "amd64"

    return (opsys, machine)
