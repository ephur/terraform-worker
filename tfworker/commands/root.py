import tempfile
from pathlib import Path
from typing import Any, Dict

import click

import tfworker.util.log as log
from tfworker.cli_options import CLIOptionsRoot

from .config import load_config, resolve_model_with_cli_options


class RootCommand:
    """
    The RootCommand class is the main entry point for the CLI.

    It is only responsible for setting up the root/global options shared by
    all sub-commands.
    """

    def __init__(self) -> None:
        """
        Initliaze the RootCommand object; this is the main entry point for the CLI.

        Args:
            args (dict, optional): A dictionary of arguments to initialize the RootCommand with. Defaults to {}.
        """
        log.trace("initializing root command object")
        app_state = click.get_current_context().obj
        options = app_state.root_options
        app_state.working_dir = self._resolve_working_dir(options.working_dir)
        log.debug(f"working directory: {app_state.working_dir}")
        log.debug(f"loading config file: {options.config_file}")
        app_state.loaded_config = load_config(
            options.config_file, self._prepare_template_vars(options)
        )
        log.safe_trace(f"loaded config: {app_state.loaded_config}")
        # update the app_config with configuration from the command line
        resolve_model_with_cli_options(app_state)
        log.trace("finished initializing root command object")

    @staticmethod
    def _resolve_working_dir(working_dir: str | None) -> Path:
        """
        Resolve the working directory.

        Args:
            working_dir (str): The working directory.

        Returns:
            pathlib.Path: The resolved working directory.
        """
        if working_dir is None:
            log.trace("working directory not provided, using temporary directory")
            return Path(tempfile.TemporaryDirectory().name)
        log.trace(f"working directory provided: {working_dir}")
        return Path(working_dir).resolve()

    @staticmethod
    def _prepare_template_vars(options: CLIOptionsRoot) -> Dict[str, Any]:
        """
        Prepare the template variables.

        Args:
            options (CLIOptionsRoot): The root options.

        Returns:
            Dict[str, Any]: The template variables.
        """
        template_items = {}
        log.trace("preparing template items")
        for k, v in options.model_dump().items():

            if v is None:
                log.trace(f"skipping {k} as it is None")
                continue

            if isinstance(v, str):
                log.trace(f"adding {k}={v}")
                template_items[k] = v
                continue

            if isinstance(v, list):
                log.trace(f"attempting to add list of strings {k}={v}")
                for i in v:
                    if isinstance(i, str):
                        subs = i.split("=")
                        if len(subs) == 2:
                            log.trace(f"adding list item {subs[0]}={subs[1]}")
                            template_items[subs[0]] = subs[1]
                        else:
                            log.trace(
                                f"skipping invalid list item {i}; not valid k=v pair"
                            )
                            continue
                    else:
                        log.trace(f"skipping {i} as it is not a string")

            log.trace(f"skipping {k} as it is not a string or list of strings")
        log.trace(f"template_items: {template_items}")
        return template_items
