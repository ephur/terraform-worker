# This file contains functions primarily used by the "TerraformCommand" class
# the goal of moving these functions here is to reduce the responsibility of
# the TerraformCommand class, making it easier to test and maintain
import pathlib
import re
import shutil
from functools import lru_cache
from typing import Dict, List, Union

import click

import tfworker.util.terraform_helpers as tfhelpers
from tfworker.constants import (
    DEFAULT_REPOSITORY_PATH,
    TF_PROVIDER_DEFAULT_HOSTNAME,
    TF_PROVIDER_DEFAULT_NAMESPACE,
)
from tfworker.providers.providers_collection import ProvidersCollection
from tfworker.types import ProviderGID
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
    if module_path == "":
        module_path = f"{DEFAULT_REPOSITORY_PATH}/terraform-modules"

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


@lru_cache
def get_terraform_version(terraform_bin: str) -> tuple[int, int]:
    """
    Get the terraform version and return the major and minor version.

    Args:
        terraform_bin (str): The path to the terraform binary.
    """

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


def mirror_providers(
    providers: ProvidersCollection, terraform_bin: str, working_dir: str, cache_dir: str
) -> None:
    """
    Mirror the providers in the cache directory.

    Args:
        providers (ProvidersCollection): The providers to mirror.
        terraform_bin (str): The path to the terraform binary.
        working_dir (str): The working directory.
        cache_dir (str): The cache directory.
    """
    click.secho(f"Mirroring providers to {cache_dir}", fg="yellow")
    tfhelpers._validate_cache_dir(cache_dir)
    try:
        with tfhelpers._write_mirror_configuration(
            providers, working_dir, cache_dir
        ) as temp_dir:
            (return_code, stdout, stderr) = pipe_exec(
                f"{terraform_bin} providers mirror {cache_dir}",
                cwd=temp_dir,
                stream_output=True,
            )
            if return_code != 0:
                click.secho(f"Unable to mirror providers\n{stderr.decode()}", fg="red")
                raise SystemExit(1)
    except IndexError:
        click.secho("All providers in cache", fg="yellow")


def generate_terraform_lockfile(
    providers: ProvidersCollection,
    included_providers: Union[None, List[str]],
    cache_dir: str,
) -> Union[None, str]:
    """
    Generate the content to put in a .terraform.lock.hcl file to lock providers using the
    cached versions, if any required providers are not in the cache, return None.

    Args:
        providers (ProvidersCollection): The providers to lock.
        included_providers (List[str] or None): The providers to include in the lockfile; if none
            is provided, all providers will be included.
        cache_dir (str): The cache directory.

    Returns:
        Union[None, str]: The content of the .terraform.lock.hcl file or None if any required providers are not in the cache
    """
    lockfile = []
    click.secho(
        f"Generating lockfile for providers: {included_providers or [x.tag for x in providers]}",
        fg="yellow",
    )
    for provider in providers:
        if tfhelpers._not_in_cache(provider.gid, provider.version, cache_dir):
            return None
        if included_providers is not None and provider.tag not in included_providers:
            continue
        lockfile.append(f'provider "{str(provider.gid)}" {{')
        lockfile.append(f'  version     = "{provider.version}"')
        lockfile.append(f'  constraints = "{provider.version}"')
        lockfile.append("  hashes = [")
        for hash in tfhelpers._get_cached_hash(
            provider.gid, provider.version, cache_dir
        ):
            lockfile.append(f'    "{hash}",')
        lockfile.append("  ]")
        lockfile.append("}")
        lockfile.append("")
    return "\n".join(lockfile)


@lru_cache
def get_provider_gid_from_source(source: str) -> ProviderGID:
    """
    Get the source address from the source string.

    See https://developer.hashicorp.com/terraform/language/providers/requirements#source-addresses
    for details.

    Args:
        source (str): The source string.

    Returns:
        ProviderGID: A named tuple containing hostname, namespace, and type.

    Raises:
        ValueError: If the source string is invalid.
    """
    if source is None or len(source) == 0:
        raise ValueError(
            f"Invalid source string, must contain between 1 and 3 parts: {source}"
        )
    parts = source.split("/")
    if len(parts) > 3 or len(parts) < 1:
        raise ValueError(
            f"Invalid source string, must contain between 1 and 3 parts: {source}"
        )

    # Assign parts with defaults for hostname and namespace
    ptype = parts[-1]
    namespace = parts[-2] if len(parts) > 1 else TF_PROVIDER_DEFAULT_NAMESPACE
    hostname = parts[-3] if len(parts) > 2 else TF_PROVIDER_DEFAULT_HOSTNAME

    return ProviderGID(hostname=hostname, namespace=namespace, type=ptype)


@lru_cache
def find_required_providers(
    search_dir: str,
) -> Union[None, Dict[str, [Dict[str, str]]]]:
    """
    Find all the required providers in the search directory.

    Args:
        search_dir (str): The directory to search.

    Returns:
        Dict[str, [Dict[str, str]]]: A dictionary of required providers, with the provider name
        as the key and the provider details as the value.
    """
    required_providers = tfhelpers._find_required_providers(search_dir)
    if len(required_providers) == 0:
        return None
    return required_providers
