import json
import pathlib
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest

from tfworker.providers import ProviderGID
from tfworker.providers.collection import ProvidersCollection
from tfworker.util.system import get_platform
from tfworker.util.terraform_helpers import (
    _create_mirror_configuration,
    _find_required_providers,
    _get_cached_hash,
    _get_provider_cache_dir,
    _not_in_cache,
    _parse_required_providers,
    _write_mirror_configuration,
)


@pytest.fixture
def provider_gid():
    return ProviderGID(hostname="example.com", namespace="namespace", type="provider")


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path


@pytest.fixture
def version():
    return "1.0.0"


@pytest.fixture
def providers_collection():
    providers_odict = {
        "provider1": {
            "requirements": {"source": "hashicorp/provider1", "version": "1.0.0"}
        },
    }
    return ProvidersCollection(
        providers_odict=providers_odict,
        authenticators=MagicMock(),
    )


@pytest.fixture
def empty_providers_collection():
    return ProvidersCollection(
        providers_odict={},
        authenticators=MagicMock(),
    )


@pytest.fixture
def create_cache_files(cache_dir, provider_gid, version):
    provider_dir = (
        pathlib.Path(cache_dir)
        / provider_gid.hostname
        / provider_gid.namespace
        / provider_gid.type
    )
    provider_dir.mkdir(parents=True, exist_ok=True)

    version_file = provider_dir / f"{version}.json"
    platform = get_platform()
    provider_file = (
        provider_dir
        / f"terraform-provider-{provider_gid.type}_{version}_{platform[0]}_{platform[1]}.zip"
    )

    version_data = {
        "archives": {f"{platform[0]}_{platform[1]}": {"hashes": "dummy_hash"}}
    }

    with open(version_file, "w") as f:
        json.dump(version_data, f)

    with open(provider_file, "w") as f:
        f.write("dummy_provider_content")

    return cache_dir, version_file, provider_file


def test_not_in_cache_false(provider_gid, version, create_cache_files):
    cache_dir, version_file, provider_file = create_cache_files
    assert not _not_in_cache(provider_gid, version, str(cache_dir))


def test_not_in_cache_true(provider_gid, version, cache_dir):
    assert _not_in_cache(provider_gid, version, str(cache_dir))


def test_not_in_cache_missing_version_file(provider_gid, version, create_cache_files):
    cache_dir, version_file, provider_file = create_cache_files
    version_file.unlink()  # Remove the version file
    assert _not_in_cache(provider_gid, version, str(cache_dir))


def test_not_in_cache_missing_provider_file(provider_gid, version, create_cache_files):
    cache_dir, version_file, provider_file = create_cache_files
    provider_file.unlink()  # Remove the provider file
    assert _not_in_cache(provider_gid, version, str(cache_dir))


def test_get_cached_hash(provider_gid, version, create_cache_files):
    cache_dir, _, _ = create_cache_files
    cached_hash = _get_cached_hash(provider_gid, version, str(cache_dir))
    assert cached_hash == "dummy_hash"


def test_get_provider_cache_dir(provider_gid, cache_dir):
    provider_cache_dir = _get_provider_cache_dir(provider_gid, str(cache_dir))
    expected_dir = (
        pathlib.Path(cache_dir)
        / provider_gid.hostname
        / provider_gid.namespace
        / provider_gid.type
    )
    assert provider_cache_dir == expected_dir


def test_write_mirror_configuration(providers_collection, cache_dir):
    with TemporaryDirectory() as working_dir:
        temp_dir = _write_mirror_configuration(
            providers_collection, working_dir, str(cache_dir)
        )
        assert temp_dir is not None
        assert (pathlib.Path(temp_dir.name) / "terraform.tf").exists()


def test_create_mirror_configuration(providers_collection):
    includes = ["provider1", "provider2"]
    tf_config = _create_mirror_configuration(providers_collection, includes)
    assert "terraform {" in tf_config


def test_parse_required_providers():
    content = {
        "terraform": [
            {
                "required_providers": [
                    {"provider1": {"source": "hashicorp/provider1", "version": "1.0.0"}}
                ]
            }
        ]
    }
    expected_providers = {
        "provider1": {"source": "hashicorp/provider1", "version": "1.0.0"}
    }
    assert _parse_required_providers(content) == expected_providers


def test_parse_required_providers_no_providers():
    content = {"terraform": [{"required_providers": []}]}
    assert _parse_required_providers(content) is None


def test_parse_required_providers_no_terraform():
    content = {
        "required_providers": [
            {"provider1": {"source": "hashicorp/provider1", "version": "1.0.0"}}
        ]
    }
    assert _parse_required_providers(content) is None


def test_find_required_providers(tmp_path):
    tf_content = """
    terraform {
      required_providers {
        provider1 = {
          source = "hashicorp/provider1"
          version = "1.0.0"
        }
      }
    }
    """
    test_file = tmp_path / "main.tf"
    with open(test_file, "w") as f:
        f.write(tf_content)

    providers = _find_required_providers(str(tmp_path))
    expected_providers = {
        "provider1": {"source": "hashicorp/provider1", "version": "1.0.0"}
    }
    assert providers == expected_providers
