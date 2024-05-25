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

import io
import os
import pathlib
import platform
import re
import shutil
import tempfile
from pathlib import Path
from typing import Union

import click
import hcl2
import jinja2
import yaml
from jinja2.runtime import StrictUndefined


class RootCommand:
    def __init__(self, args={}):
        """
        Initialize the RootCommand with the given arguments.

        Args:
            args (dict, optional): A dictionary of arguments to initialize the RootCommand with. Defaults to {}.
        """
        self.working_dir = args.get("working_dir", None)

        # the default behavior of --clean/--no-clean varies depending on if working-dir is passed
        defaultClean = False if (self.working_dir is not None) else True
        if args.get("clean", None) is None:
            self.clean = defaultClean
        else:
            self.clean = args.get("clean")

        if self.working_dir is not None:
            self.temp_dir = pathlib.Path(self.working_dir).resolve()
        else:
            self.temp_dir = tempfile.mkdtemp()

        self.args = self.StateArgs()
        self.config_file = args.get("config_file")

        # Config accessors
        self.tf = None
        self.add_args(args)

    def __del__(self):
        """
        Cleanup the temporary directory after execution.
        """

        if self.clean:
            # the affect of remove_top being true is removing the top level directory, for a temporary
            # directory this is desirable however when a working-dir is specified it's likely a volume
            # mount in a container, so empty the files if clean is desired but do not remove the top level
            remove_top = True if self.working_dir is None else False

            try:
                rm_tree(self.temp_dir, inner=remove_top)
            except FileNotFoundError:
                pass

    def add_args(self, args):
        """
        Add a dictionary of args.

        Args:
            args (dict): A dictionary of arguments to add.
        """
        for k, v in args.items():
            self.add_arg(k, v)

    def add_arg(self, k, v):
        """
        Add an argument to the state args.

        Args:
            k (str): The key of the argument.
            v (any): The value of the argument.
        """
        setattr(self.args, k, v)
        return None

    def load_config(self):
        """
        Load the configuration file.
        """
        if not self.config_file:
            return

        self._config_file_exists()
        rendered_config = self._process_template()

        if self.config_file.endswith(".hcl"):
            self.config = ordered_config_load_hcl(rendered_config)
        else:
            self.config = ordered_config_load(rendered_config)

        # Decorate the RootCommand with the config values
        self.tf = self.config.get("terraform", dict())
        self._pullup_keys()
        self._merge_args()

    def _config_file_exists(self):
        """
        Check if the configuration file exists.
        """
        if not os.path.exists(self.config_file):
            click.secho(
                f"configuration file does not exist: {self.config_file}", fg="red"
            )
            raise SystemExit(1)

    def _process_template(self) -> str:
        """
        Process the Jinja2 template.
        """
        try:
            template_reader = io.StringIO()
            jinja_env = jinja2.Environment(
                undefined=StrictUndefined,
                loader=jinja2.FileSystemLoader(
                    pathlib.Path(self.config_file).parents[0]
                ),
            )
            template_config = jinja_env.get_template(
                pathlib.Path(self.config_file).name
            )
            template_config.stream(
                **self.args.template_items(return_as_dict=True, get_env=True)
            ).dump(template_reader)
        except jinja2.exceptions.UndefinedError as e:
            click.secho(
                f"configuration file contains invalid template substitutions: {e}",
                fg="red",
            )
            raise SystemExit(1)

        return template_reader.getvalue()

    def _pullup_keys(self):
        """
        A utility function to place keys from the loaded config file directly on the RootCommand instance.
        """
        for k in [
            "definitions",
            "providers",
            "handlers",
            "remote_vars",
            "template_vars",
            "terraform_vars",
            "worker_options",
        ]:
            if self.tf:
                setattr(self, f"{k}_odict", self.tf.get(k, dict()))
            else:
                setattr(self, f"{k}_odict", None)

    def _merge_args(self):
        """
        Merge the worker options from the config file with the command line arguments.
        """
        for k, v in self.worker_options_odict.items():
            self.add_arg(k, v)

    class StateArgs:
        """
        A class to hold arguments in the state for easier access.
        """

        def __iter__(self):
            return iter(self.__dict__)

        def __getitem__(self, name):
            return self.__dict__[name]

        def __repr__(self):
            return str(self.__dict__)

        def keys(self):
            return self.__dict__.keys()

        def items(self):
            return self.__dict__.items()

        def values(self):
            return self.__dict__.values()

        def template_items(self, return_as_dict=False, get_env=False):
            rvals = {}
            for k, v in self.__dict__.items():
                if k == "config_var":
                    try:
                        rvals["var"] = get_config_var_dict(v)
                    except ValueError as e:
                        click.secho(
                            f'Invalid config-var specified: "{e}" must be in format key=value',
                            fg="red",
                        )
                        raise SystemExit(1)
                else:
                    rvals[k] = v
            if get_env is True:
                rvals["env"] = dict()
                for k, v in os.environ.items():
                    rvals["env"][k] = v
            if return_as_dict:
                return rvals
            return rvals.items()


def get_config_var_dict(config_vars):
    """
    Returns a dictionary of of key=value for each item provided as a command line substitution.

    Args:
        config_vars (list): A list of command line substitutions.

    Returns:
        dict: A dictionary of key=value pairs.
    """
    return_vars = dict()
    for cv in config_vars:
        try:
            k, v = tuple(cv.split("="))
            return_vars[k] = v
        except ValueError:
            raise ValueError(cv)
    return return_vars


def ordered_config_load_hcl(config: str) -> dict:
    """
    Load an hcl config, and replace templated items.
    """
    return hcl2.loads(config)


def ordered_config_load(config: str) -> dict:
    """
    since python 3.7 the yaml loader is deterministic, so we can
    use the standard yaml loader
    """
    try:
        return yaml.load(config, Loader=yaml.FullLoader)
    except yaml.YAMLError as e:
        click.secho(f"error loading yaml/json: {e}", fg="red")
        click.secho(f"the configuration that caused the error was\n:", fg="red")
        for i, line in enumerate(config.split("\n")):
            click.secho(f"{i+1}: {line}", fg="red")
        raise SystemExit(1)


def get_platform():
    """
    Returns a formatted operating system / architecture tuple that is consistent with common distribution creation tools.

    Returns:
        tuple: A tuple containing the operating system and architecture.
    """

    # strip off "2" which only appears on old linux kernels
    opsys = platform.system().rstrip("2").lower()

    # make sure machine uses consistent format
    machine = platform.machine()
    if machine == "x86_64":
        machine = "amd64"

    # some 64 bit arm extensions will report aarch64, this is functionaly
    # equivalent to arm64 which is recognized and the pattern used by the TF
    # community
    if machine == "aarch64":
        machine = "arm64"
    return (opsys, machine)


def rm_tree(base_path: Union[str, Path], inner: bool = False) -> None:
    """
    Recursively removes all files and directories.

    Args:
        base_path (Union[str, Path]): The base path to start removing files and directories from.
        inner (bool, optional): Controls recrusion, if True only the inner files and directories are removed. Defaults to False.
    """
    parent: Path = Path(base_path)

    for child in parent.glob("*"):
        if child.is_file() or child.is_symlink():
            child.unlink()
        else:
            rm_tree(child, inner=True)
    if inner:
        parent.rmdir()
