import json
import os
import pathlib
import re
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Dict, List, Union

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
                content = _load_tf_file(f"{root}/{file}")
                if content is not None:
                    _update_parsed_providers(
                        providers, _parse_required_providers(content)
                    )
    log.trace(
        f"Found required providers: {[x for x in providers.keys()]} in {search_dir}"
    )
    return providers


def _find_loaded_required_providers(
    search_dir: str,
) -> Dict[str, Dict[str, Union[str, SpecifierSet]]]:
    """
    Find the providers terraform will require for the configuration in search_dir.

    Terraform reads the root module's own .tf files, and for each module call only
    the directory modules.json records for it. A git source with a //subdir
    fetches the whole repository, so walking the tree finds required_providers
    terraform never loads, and a lockfile naming them fails
    `terraform init -lockfile=readonly`. Provider blocks count too, since
    terraform requires a provider for each one it is configured with.

    Falls back to walking search_dir when modules have not been fetched yet.

    Args:
        search_dir (str): The root module directory, after `terraform get`.

    Returns:
        Dict[str, Dict[str, Union[str, SpecifierSet]]]: Each provider's "source"
        and "version"; a provider known only from a provider block has an empty
        source and no version constraint.

    Raises:
        TFWorkerException: If modules.json cannot be read or is not the expected shape.
    """
    manifest = os.path.join(search_dir, ".terraform", "modules", "modules.json")
    if not os.path.isfile(manifest):
        return _find_required_providers(search_dir)

    module_dirs = {"."} | set(_read_module_dirs(manifest))

    providers = {}
    # added after every required_providers entry is merged, so an empty source
    # from a provider block is never compared against a declared one
    configured = set()
    for module_dir in sorted(module_dirs):
        path = os.path.normpath(os.path.join(search_dir, module_dir))
        if not os.path.isdir(path):
            log.debug(f"module directory {path} from {manifest} does not exist")
            continue
        for file in sorted(os.listdir(path)):
            if not file.endswith(".tf"):
                continue
            content = _load_tf_file(os.path.join(path, file))
            if content is None:
                continue
            _update_parsed_providers(providers, _parse_required_providers(content))
            configured.update(_parse_provider_blocks(content))
    for name in sorted(configured - providers.keys()):
        providers[name] = {"source": "", "version": _get_specifier_set("")}
    log.trace(
        f"Found loaded required providers: {[x for x in providers.keys()]} in {search_dir}"
    )
    return providers


def _read_module_dirs(manifest: str) -> List[str]:
    """The module directories recorded in a modules.json manifest."""
    try:
        with open(manifest, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise TFWorkerException(f"unable to read module manifest {manifest}: {e}")

    modules = data.get("Modules") if isinstance(data, dict) else None
    if not isinstance(modules, list) or not all(isinstance(m, dict) for m in modules):
        raise TFWorkerException(
            f"module manifest {manifest} has no list of modules under 'Modules'"
        )
    return [m["Dir"] for m in modules if isinstance(m.get("Dir"), str) and m["Dir"]]


def _load_tf_file(path: str) -> Union[dict, None]:
    """Parse a .tf file, or return None if it cannot be parsed."""
    with open(path, "r") as f:
        try:
            return hcl2.load(f)
        except UnexpectedToken as e:
            # Unparsable .tf files are routine here: the walk covers vendored
            # module trees, and a file this parser chokes on is not necessarily
            # one terraform rejects, so this is detail for a debug run.
            log.debug(
                f"not processing {path} for required providers; HCL parsing error: {e}"
            )
            return None


def _parse_provider_blocks(content: dict) -> List[str]:
    """The names of the providers configured by provider blocks in the content."""
    return [name for block in content.get("provider", []) for name in block]


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

    # Support Terraform's pessimistic operator '~>' by expanding it
    # to an equivalent lower/upper bound pair compatible with
    # packaging's SpecifierSet.
    #
    # Semantics implemented (matching Terraform docs):
    #   - '~> X'        -> '>= X, < X+1'
    #   - '~> X.Y'      -> '>= X.Y, < X+1'
    #   - '~> X.Y.Z'    -> '>= X.Y.Z, < X.(Y+1).0'
    # Multiple constraints may be comma-separated and combined.
    def _expand_pessimistic(spec: str) -> str:
        if "~>" not in spec:
            return spec

        # Split on commas to process each constraint atomically
        parts = [p.strip() for p in spec.split(",") if p.strip()]
        expanded_parts: list[str] = []
        pess_pattern = re.compile(r"^~>\s*(\d+(?:\.\d+){0,2})\s*$")

        for p in parts:
            m = pess_pattern.match(p)
            if not m:
                # leave non-pessimistic or unsupported tokens as-is
                expanded_parts.append(p)
                continue

            ver = m.group(1)
            nums = [int(x) for x in ver.split(".")]
            # Determine upper bound based on number of components
            if len(nums) >= 3:
                major, minor = nums[0], nums[1]
                upper = f"{major}.{minor + 1}.0"
            else:
                # For '~> X' or '~> X.Y' allow any version before next major
                upper = f"{nums[0] + 1}"

            # Lower bound is the original version verbatim
            expanded_parts.append(f">={ver}")
            expanded_parts.append(f"<{upper}")

        return ",".join(expanded_parts)

    # First, try to parse as-is (normal packaging-compatible spec)
    try:
        return SpecifierSet(version)
    except InvalidSpecifier:
        # Try expanding '~>' pessimistic constraints
        try:
            expanded = _expand_pessimistic(version)
            return SpecifierSet(expanded)
        except InvalidSpecifier:
            # Fallback: if it's a bare version pin like '1.2.3'
            try:
                return SpecifierSet(f"=={version}")
            except InvalidSpecifier:
                raise TFWorkerException(f"Invalid version specifier: {version}")


def specifier_to_terraform(spec: Union[str, SpecifierSet]) -> str:
    """
    Convert a Python packaging specifier or string into a Terraform-compatible
    version constraint string.

    Notes:
    - Terraform accepts operators like =, !=, >, >=, <, <=, and ~>.
    - The Python packaging module uses '==' for equality; Terraform expects '='.
    - For non-specifier strings (including '~> ...'), return as-is.

    Args:
        spec (Union[str, SpecifierSet]): The specifier to convert.

    Returns:
        str: A Terraform-compatible constraint string.
    """
    # If it's already a string, only normalize equality syntax if present
    if isinstance(spec, str):
        return spec.replace("===", "=").replace("==", "=")

    # For SpecifierSet, stringify and then normalize '==' to '='
    s = str(spec)
    return s.replace("===", "=").replace("==", "=")


def ensure_concrete_version_for_lockfile(version: str) -> str:
    """
    Validate that a version string is a single, concrete version suitable for
    the lockfile's `version` field (not a constraint expression).

    Accepts common semver forms, including pre-release or build metadata
    (e.g., 1.2.3, 1.2.3-alpha.1, 1.2.3+build.1).

    Raises TFWorkerException if the value looks like a constraint
    (contains operators such as <, >, !, =, ~, or comma-separated ranges),
    or doesn't resemble a version.
    """
    s = version.strip()
    # Fast checks for constraint operators or lists
    if any(ch in s for ch in ("<", ">", "!", "=", "~", ",")):
        raise TFWorkerException(
            f"Provider version must be a single version for lockfile, not a constraint: {version}"
        )
    if " " in s:
        raise TFWorkerException(
            f"Provider version must be a single version for lockfile (no spaces): {version}"
        )

    # Loose semver-ish validation: digits with dot groups, optional pre-release/build
    # examples: 1.2.3, 1.2.3-alpha.1, 1.0.0+build.1, 1.2
    pattern = re.compile(r"^\d+(?:\.\d+)*(?:[-+][0-9A-Za-z\.-]+)?$")
    if not pattern.match(s):
        raise TFWorkerException(
            f"Provider version must be a concrete version for lockfile: {version}"
        )
    return s
