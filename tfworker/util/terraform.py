# This file contains functions primarily used by the "TerraformCommand" class
# the goal of moving these functions here is to reduce the responsibility of
# the TerraformCommand class, making it easier to test and maintain
import pathlib
import shutil

import click


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
