import json
import os
import pathlib
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Dict, List

import hcl2
from lark.exceptions import UnexpectedToken
from packaging.specifiers import InvalidSpecifier, SpecifierSet

import tfworker.util.log as log
from tfworker.exceptions import TFWorkerException
from tfworker.util.system import get_platform

if TYPE_CHECKING:
    from tfworker.providers.collection import (  # pragma: no cover  # noqa: F401
        ProvidersCollection,
    )
    from tfworker.providers.model import (  # pragma: no cover  # noqa: F401
        ProviderGID,
        ProviderRequirements,
    )


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
    includes = [
        x.name
        for x in providers.values()
        if _not_in_cache(x.gid, x.config.requirements.version, cache_dir)
    ]

    if len(includes) == 0:
        raise IndexError("No providers to mirror")

    log.info(f"mirroring providers: {', '.join(includes)}")
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


def _find_required_providers(
    search_dir: str,
) -> Dict[str, Dict[str, "ProviderRequirements"]]:
    """
    Find all of the specified required providers in the search directory.

    Args:
        search_dir (str): The directory to search for required providers.

    Returns:
        Dict[str, Dict[str, ProviderRequirements]]: A dictionary of required providers.
    """
    providers = {}
    for root, _, files in os.walk(search_dir, followlinks=True):
        for file in files:
            if file.endswith(".tf"):
                with open(f"{root}/{file}", "r") as f:
                    try:
                        content = hcl2.load(f)
                    except UnexpectedToken as e:
                        log.info(
                            f"not processing {root}/{file} for required providers; see debug output for HCL parsing errors"
                        )
                        log.debug(f"HCL processing errors in {root}/{file}: {e}")
                        continue
                    _update_parsed_providers(
                        providers, _parse_required_providers(content)
                    )
    log.trace(
        f"Found required providers: {[x for x in providers.keys()]} in {search_dir}"
    )
    return providers


def _parse_required_providers(content: dict) -> Dict[str, "ProviderRequirements"]:
    """
    Parse the required providers from the content.

    Args:
        content (dict): The content to parse.

    Returns:
        Dict[str, Dict[str, str]]: The required providers.
    """
    if "terraform" not in content:
        return {}

    providers = {}
    terraform_blocks = content["terraform"]

    for block in terraform_blocks:
        if "required_providers" in block:
            for required_provider in block["required_providers"]:
                for k, v in required_provider.items():
                    providers[k] = v
    return providers


def _update_parsed_providers(providers: dict, parsed_providers: dict):
    """
    Update the providers with the parsed providers.

    Args:
        providers (dict): The providers to update.
        parsed_providers (dict): The parsed providers to update with.

    Raises:
        TFWorkerException: If there are conflicting sources for the same provider.
    """
    for k, v in parsed_providers.items():
        if k not in providers:
            new_provider = {
                "source": v.get("source", ""),
                "version": _get_specifier_set(v.get("version", "")),
            }
            providers[k] = new_provider
            continue
        if v.get("source") is not None and providers[k].get("source") is not None:
            if v["source"] != providers[k]["source"]:
                raise TFWorkerException(
                    f"provider {k} has conflicting sources: {v['source']} and {providers[k]['source']}"
                )
        if v.get("version") is not None:
            providers[k]["version"] = providers[k]["version"] & _get_specifier_set(
                v["version"]
            )
    return providers


def _get_specifier_set(version: str) -> SpecifierSet:
    """
    Get the SpecifierSet for the version.

    Args:
        version (str): The version to get the SpecifierSet for.

    Returns:
        SpecifierSet: The SpecifierSet for the version.
    """
    try:
        return SpecifierSet(version)
    except InvalidSpecifier:
        try:
            return SpecifierSet(f"=={version}")
        except InvalidSpecifier:
            raise TFWorkerException(f"Invalid version specifier: {version}")
