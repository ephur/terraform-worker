# This file contains functions primarily used by the "TerraformCommand" class
# the goal of moving these functions here is to reduce the responsibility of
# the TerraformCommand class, making it easier to test and maintain
import json
import os
import pathlib
import re
import shutil
from functools import lru_cache
from tempfile import TemporaryDirectory
from typing import Dict, List, Union

import click
import hcl2

from tfworker.constants import (
    DEFAULT_REPOSITORY_PATH,
    TF_PROVIDER_DEFAULT_HOSTNAME,
    TF_PROVIDER_DEFAULT_NAMESPACE,
)
from tfworker.providers.providers_collection import ProvidersCollection
from tfworker.types import ProviderGID
from tfworker.util.system import get_platform, pipe_exec


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
    _validate_cache_dir(cache_dir)
    try:
        with _write_mirror_configuration(providers, working_dir, cache_dir) as temp_dir:
            (return_code, stdout, stderr) = pipe_exec(
                f"{terraform_bin} providers mirror {cache_dir}",
                cwd=temp_dir,
                stream_output=True,
            )
            if return_code != 0:
                click.secho(f"Unable to mirror providers\n{stderr.decode()}", fg="red")
                raise SystemExit(1)

            # after mirroring the providers, copy the lock file to the provider mirror
            # so it can be stored, and ensure providers are not downloaded with each run
            lock_file = pathlib.Path(temp_dir) / ".terraform.lock"
            if lock_file.exists():
                shutil.copy(lock_file, cache_dir)
    except IndexError:
        click.secho("All providers in cache", fg="yellow")


def generate_terraform_lockfile(
    providers: ProvidersCollection, cache_dir: str
) -> Union[None, str]:
    """
    Generate the content to put in a .terraform.lock.hcl file to lock providers using the
    cached versions, if any required providers are not in the cache, return None.

    Args:
        providers (ProvidersCollection): The providers to lock.
        cache_dir (str): The cache directory.

    Returns:
        Union[None, str]: The content of the .terraform.lock.hcl file or None if any required providers are not in the cache
    """
    lockfile = []
    for provider in providers:
        if _not_in_cache(provider.gid, provider.version, cache_dir):
            return None

        lockfile.append(f'provider "{str(provider.gid)}" {{')
        lockfile.append(f'  version     = "{provider.version}"')
        lockfile.append(f'  constraints = "{provider.version}"')
        lockfile.append("  hashes = [")
        # {str(_get_cached_hash(provider.gid, provider.version, cache_dir))}')
        for hash in _get_cached_hash(provider.gid, provider.version, cache_dir):
            lockfile.append(f'    "{hash}",')
        lockfile.append("  ]")
        lockfile.append("}")
        lockfile.append("")
    return "\n".join(lockfile)


def _not_in_cache(gid: ProviderGID, version: str, cache_dir: str) -> bool:
    """
    Check if the provider is not in the cache directory.

    Args:
        provider (str): The provider to check.
        cache_dir (str): The cache directory.

    Returns:
        bool: True if the provider is not in the cache directory.
    """
    provider_dir = pathlib.Path(cache_dir) / gid.hostname / gid.namespace / gid.type
    platform = get_platform()
    if not provider_dir.exists():
        return True

    # look for version.json and terraform-provider-_version_platform.zip in the provider directory
    version_file = provider_dir / f"{version}.json"
    provider_file = (
        provider_dir
        / f"terraform-provider-{gid.type}_{version}_{platform[0]}_{platform[1]}.zip"
    )
    if not version_file.exists() or not provider_file.exists():
        return True
    return False


def _get_cached_hash(gid: ProviderGID, version: str, cache_dir: str) -> str:
    """
    Get the hash of the cached provider.

    Args:
        provider (str): The provider to get the hash for.
        cache_dir (str): The cache directory.

    Returns:
        str: The hash of the cached provider.

    Raises:
        ValueError: If the provider hash can not be determined but the file is present
    """
    provider_dir = _get_provider_cache_dir(gid, cache_dir)
    version_file = provider_dir / f"{version}.json"
    with open(version_file, "r") as f:
        hash_data = json.load(f)

    platform = get_platform()

    return hash_data["archives"][f"{platform[0]}_{platform[1]}"]["hashes"]


def _write_mirror_configuration(
    providers: ProvidersCollection, working_dir: str, cache_dir: str
) -> TemporaryDirectory:
    """
    Write the mirror configuration to a temporary directory in the working directory.

    Args:
        providers (ProvidersCollection): The providers to mirror.
        working_dir (str): The working directory.

    Returns:
        TemporaryDirectory: A temporary directory containing the mirror configuration.

    Raises:
        IndexError: If there are no providers to mirror.
    """
    includes = [x for x in providers if _not_in_cache(x.gid, x.version, cache_dir)]
    if len(includes) == 0:
        raise IndexError("No providers to mirror")
    click.secho(f"Mirroring providers: {includes}", fg="yellow")

    mirror_configuration = _create_mirror_configuration(
        providers=providers, includes=includes
    )
    temp_dir = TemporaryDirectory(dir=working_dir)
    mirror_file = pathlib.Path(temp_dir.name) / "terraform.tf"
    with open(mirror_file, "w") as f:
        f.write(mirror_configuration)
    return temp_dir


def _create_mirror_configuration(
    providers: ProvidersCollection, includes: List[str] = []
) -> str:
    """
    Generate a terraform configuration file with all of the providers
    to mirror.
    """
    tf_string = []
    tf_string.append("terraform {")
    tf_string.append(providers.required_hcl(includes=includes))
    tf_string.append("}")
    return "\n".join(tf_string)


def _validate_cache_dir(cache_dir: str) -> None:
    """
    Validate the cache directory, it should exist and be writable.

    Args:
        cache_dir (str): The cache directory.
    """
    cache_dir = pathlib.Path(cache_dir)
    if not cache_dir.exists():
        click.secho(f"Cache directory {cache_dir} does not exist", fg="red")
        raise SystemExit(1)
    if not cache_dir.is_dir():
        click.secho(f"Cache directory {cache_dir} is not a directory", fg="red")
        raise SystemExit(1)
    if not os.access(cache_dir, os.W_OK):
        click.secho(f"Cache directory {cache_dir} is not writable", fg="red")
        raise SystemExit(1)
    if not os.access(cache_dir, os.R_OK):
        click.secho(f"Cache directory {cache_dir} is not readable", fg="red")
        raise SystemExit(1)
    if not os.access(cache_dir, os.X_OK):
        click.secho(f"Cache directory {cache_dir} is not executable", fg="red")
        raise SystemExit(1)


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


def _get_provider_cache_dir(gid: ProviderGID, cache_dir: str) -> str:
    """
    Get the cache directory for a provider.

    Args:
        gid (ProviderGID): The provider GID.
        cache_dir (str): The cache directory.

    Returns:
        str: The cache directory for the provider.
    """
    return pathlib.Path(cache_dir) / gid.hostname / gid.namespace / gid.type


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
    required_providers = _find_required_providers(search_dir)
    if len(required_providers) == 0:
        return None
    return required_providers


def _find_required_providers(search_dir: str) -> Dict[str, [Dict[str, str]]]:
    providers = {}
    for root, _, files in os.walk(search_dir):
        for file in files:
            if file.endswith(".tf"):
                with open(f"{root}/{file}", "r") as f:
                    content = hcl2.load(f)
                    new_providers = _parse_required_providers(content)
                    if new_providers is not None:
                        providers.update(new_providers)
    return providers


def _parse_required_providers(content: dict) -> Union[None, Dict[str, Dict[str, str]]]:
    if "terraform" not in content:
        return None

    providers = {}
    terraform_blocks = content["terraform"]

    for block in terraform_blocks:
        if "required_providers" in block:
            for required_provider in block["required_providers"]:
                for k, v in required_provider.items():
                    providers[k] = v

    if len(providers.keys()) == 0:
        return None

    return providers
