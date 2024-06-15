# This file contains functions primarily used by the "TerraformCommand" class
# the goal of moving these functions here is to reduce the responsibility of
# the TerraformCommand class, making it easier to test and maintain
import pathlib
import re
import shutil

import click

from tfworker.constants import DEFAULT_REPOSITORY_PATH
from tfworker.util.system import pipe_exec


def prep_modules(
    module_path: str,
    target_path: str,
    ignore_patterns: list[str] = None,
    required: bool = False,
) -> None:
    """This puts any terraform modules from the module path in place. By default
    it will not generate an error if the module path is not found. If required
    is set to True, it will raise an error if the module path is not found.

    Args:
        module_path (str): The path to the terraform modules directory
        target_path (str): The path to the target directory, /terraform-modules will be appended
        ignore_patterns (list(str)): A list of patterns to ignore
        required (bool): If the terraform modules directory is required
    """
    module_path = (
        module_path
        if module_path != ""
        else f"{DEFAULT_REPOSITORY_PATH}/terraform-modules"
    )
    module_path = pathlib.Path(module_path)
    target_path = pathlib.Path(f"{target_path}/terraform-modules".replace("//", "/"))

    if not module_path.exists() and required:
        click.secho(
            f"The specified terraform-modules directory '{module_path}' does not exists",
            fg="red",
        )
        raise SystemExit(1)

    if not module_path.exists():
        return

    if ignore_patterns is None:
        ignore_patterns = ["test", ".terraform", "terraform.tfstate*"]

    click.secho(f"copying modules from {module_path} to {target_path}", fg="yellow")
    shutil.copytree(
        module_path,
        target_path,
        symlinks=True,
        ignore=shutil.ignore_patterns(*ignore_patterns),
    )


def get_terraform_version(terraform_bin):
    (return_code, stdout, stderr) = pipe_exec(f"{terraform_bin} version")
    if return_code != 0:
        click.secho(f"unable to get terraform version\n{stderr}", fg="red")
        raise SystemExit(1)
    version = stdout.decode("UTF-8").split("\n")[0]
    version_search = re.search(r".*\s+v(\d+)\.(\d+)\.(\d+)", version)
    if version_search:
        click.secho(
            f"Terraform Version Result: {version}, using major:{version_search.group(1)}, minor:{version_search.group(2)}",
            fg="yellow",
        )
        return (int(version_search.group(1)), int(version_search.group(2)))
    else:
        click.secho(f"unable to get terraform version\n{stderr}", fg="red")
        raise SystemExit(1)
