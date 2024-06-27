import json
import os
import pathlib
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Dict, List, Union

import click
import hcl2
from lark.exceptions import UnexpectedToken

from tfworker.util.system import get_platform

if TYPE_CHECKING:
    from tfworker.providers.collection import ProvidersCollection

from tfworker.types.provider import ProviderGID


def _not_in_cache(gid: "ProviderGID", version: str, cache_dir: str) -> bool:
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


def _get_cached_hash(gid: "ProviderGID", version: str, cache_dir: str) -> str:
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
    providers: "ProvidersCollection", working_dir: str, cache_dir: str
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
    includes = [x.tag for x in providers if _not_in_cache(x.gid, x.version, cache_dir)]
    if len(includes) == 0:
        raise IndexError("No providers to mirror")

    mirror_configuration = _create_mirror_configuration(
        providers=providers, includes=includes
    )
    temp_dir = TemporaryDirectory(dir=working_dir)
    mirror_file = pathlib.Path(temp_dir.name) / "terraform.tf"
    with open(mirror_file, "w") as f:
        f.write(mirror_configuration)
    return temp_dir


def _create_mirror_configuration(
    providers: "ProvidersCollection", includes: List[str] = []
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


def _get_provider_cache_dir(gid: "ProviderGID", cache_dir: str) -> str:
    """
    Get the cache directory for a provider.

    Args:
        gid (ProviderGID): The provider GID.
        cache_dir (str): The cache directory.

    Returns:
        str: The cache directory for the provider.
    """
    return pathlib.Path(cache_dir) / gid.hostname / gid.namespace / gid.type


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


def _find_required_providers(search_dir: str) -> Dict[str, [Dict[str, str]]]:
    providers = {}
    for root, _, files in os.walk(search_dir, followlinks=True):
        for file in files:
            if file.endswith(".tf"):
                with open(f"{root}/{file}", "r") as f:
                    try:
                        content = hcl2.load(f)
                    except UnexpectedToken as e:
                        click.secho(f"skipping {root}/{file}: {e}", fg="blue")
                        continue
                    new_providers = _parse_required_providers(content)
                    if new_providers is not None:
                        providers.update(new_providers)
    return providers
