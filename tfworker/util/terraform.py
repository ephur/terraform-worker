# This file contains functions primarily used by the "TerraformCommand" class
# the goal of moving these functions here is to reduce the responsibility of
# the TerraformCommand class, making it easier to test and maintain
import re
from functools import lru_cache
from typing import Dict, List, Union

import click

import tfworker.util.log as log
import tfworker.util.terraform_helpers as tfhelpers
from tfworker.constants import (
    TF_PROVIDER_DEFAULT_HOSTNAME,
    TF_PROVIDER_DEFAULT_NAMESPACE,
)
from tfworker.exceptions import TFWorkerException
from tfworker.providers import Provider, ProviderGID, ProvidersCollection
from tfworker.util.system import pipe_exec


@lru_cache
def get_terraform_version(terraform_bin: str, validation=False) -> tuple[int, int]:
    """
    Get the terraform version and return the major and minor version.

    Args:
        terraform_bin (str): The path to the terraform binary.
        validation (bool, optional): A boolean indicating if the function should raise an error if the version cannot be determined. Defaults to False.
    """

    # @TODO: instead of exiting, raise an error to handle it in the caller
    def click_exit():
        log.error(f"unable to get terraform version from {terraform_bin} version")
        click.get_current_context().exit(1)

    def validation_exit():
        raise ValueError(
            f"unable to get terraform version from {terraform_bin} version"
        )

    (return_code, stdout, stderr) = pipe_exec(f"{terraform_bin} version")
    if return_code != 0:
        if validation:
            validation_exit()
        click_exit()
    version = stdout.decode("UTF-8").split("\n")[0]
    version_search = re.search(r".*\s+v(\d+)\.(\d+)\.(\d+)", version)
    if version_search:
        log.debug(
            f"Terraform Version Result: {version}, using major:{version_search.group(1)}, minor:{version_search.group(2)}",
        )
        return (int(version_search.group(1)), int(version_search.group(2)))
    else:
        if validation:
            validation_exit()
        click_exit()


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
    log.debug(f"Mirroring providers to {cache_dir}")
    try:
        with tfhelpers._write_mirror_configuration(
            providers, working_dir, cache_dir
        ) as temp_dir:
            (return_code, _, stderr) = pipe_exec(
                f"{terraform_bin} providers mirror {cache_dir}",
                cwd=temp_dir,
                stream_output=True,
            )
            if return_code != 0:
                raise TFWorkerException(f"Unable to mirror providers: {stderr}")
    except IndexError:
        log.debug("All providers in cache")


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
    provider: Provider

    log.trace(
        f"generating lockfile for providers: {included_providers or [x.name for x in providers.values()]}"
    )
    for provider in providers.values():
        log.trace(f"checking provider {provider} / {provider.gid}")
        if tfhelpers._not_in_cache(
            provider.gid, provider.config.requirements.version, cache_dir
        ):
            log.trace(
                f"Provider {provider.gid} not in cache, skipping lockfile generation"
            )
            return None
        if included_providers is not None and provider.name not in included_providers:
            log.trace(
                f"Provider {provider.gid} not in included_providers, not adding to lockfile"
            )
            continue
        log.trace(f"Provider {provider.gid} is in cache, adding to lockfile")
        lockfile.append(f'provider "{str(provider.gid)}" {{')
        lockfile.append(f'  version     = "{provider.config.requirements.version}"')
        lockfile.append(f'  constraints = "{provider.config.requirements.version}"')
        lockfile.append("  hashes = [")
        for hash in tfhelpers._get_cached_hash(
            provider.gid, provider.config.requirements.version, cache_dir
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


# @lru_cache
def find_required_providers(
    search_dir: str,
) -> Union[None, Dict[str, List[Dict[str, str]]]]:
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
