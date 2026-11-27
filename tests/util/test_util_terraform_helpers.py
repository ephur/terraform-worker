import json
import pathlib
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest
from packaging.specifiers import SpecifierSet

from tfworker.exceptions import TFWorkerException
from tfworker.providers import ProviderGID
from tfworker.providers.collection import ProvidersCollection
from tfworker.util.system import get_platform
from tfworker.util.terraform import generate_terraform_lockfile
from tfworker.util.terraform_helpers import (
    _create_mirror_configuration,
    _find_loaded_required_providers,
    _find_required_providers,
    _get_cached_hash,
    _get_provider_cache_dir,
    _get_specifier_set,
    _not_in_cache,
    _parse_required_providers,
    _write_mirror_configuration,
    specifier_to_terraform,
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


@pytest.fixture(scope="function")
def providers_collection():
    """A ProvidersCollection object with one provider"""
    providers_odict = {
        "provider1": {
            "requirements": {"source": "hashicorp/provider1", "version": "1.0.0"}
        },
    }
    yield ProvidersCollection(
        providers_odict=providers_odict,
        authenticators=MagicMock(),
    )
    ProvidersCollection.delete_instance()


@pytest.fixture(scope="function")
def empty_providers_collection():
    """An empty ProvidersCollection object"""
    yield ProvidersCollection(
        providers_odict={},
        authenticators=MagicMock(),
    )
    ProvidersCollection.delete_instance()


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


class TestTerraformHelpersNotInCache:
    def test_not_in_cache_false(self, provider_gid, version, create_cache_files):
        cache_dir, _, _ = create_cache_files
        assert not _not_in_cache(provider_gid, version, str(cache_dir))

    def test_not_in_cache_true(self, provider_gid, version, cache_dir):
        assert _not_in_cache(provider_gid, version, str(cache_dir))

    def test_not_in_cache_missing_version_file(
        self, provider_gid, version, create_cache_files
    ):
        cache_dir, version_file, _ = create_cache_files
        version_file.unlink()  # Remove the version file
        assert _not_in_cache(provider_gid, version, str(cache_dir))

    def test_not_in_cache_missing_provider_file(
        self, provider_gid, version, create_cache_files
    ):
        cache_dir, _, provider_file = create_cache_files
        provider_file.unlink()  # Remove the provider file
        assert _not_in_cache(provider_gid, version, str(cache_dir))


class TestTerraformHelpersGetCachedHash:
    def test_get_cached_hash(self, provider_gid, version, create_cache_files):
        cache_dir, _, _ = create_cache_files
        cached_hash = _get_cached_hash(provider_gid, version, str(cache_dir))
        assert cached_hash == "dummy_hash"


class TestTerraformHelpersGetProviderCacheDir:
    def test_get_provider_cache_dir(self, provider_gid, cache_dir):
        provider_cache_dir = _get_provider_cache_dir(provider_gid, str(cache_dir))
        expected_dir = (
            pathlib.Path(cache_dir)
            / provider_gid.hostname
            / provider_gid.namespace
            / provider_gid.type
        )
        assert provider_cache_dir == expected_dir


class TestTerraformHelpersWriteMirrorConfiguration:
    def test_write_mirror_configuration(self, providers_collection, cache_dir):
        with TemporaryDirectory() as working_dir:
            temp_dir = _write_mirror_configuration(
                providers_collection, working_dir, str(cache_dir)
            )
            del providers_collection  # this is a singleton and we need to delete it to test the empty_providers_collection
            assert temp_dir is not None
            assert (pathlib.Path(temp_dir.name) / "terraform.tf").exists()

    def test_write_mirror_configuration_empty_providers(
        self, empty_providers_collection, cache_dir
    ):
        with TemporaryDirectory() as working_dir:
            with pytest.raises(IndexError):
                _write_mirror_configuration(
                    empty_providers_collection, working_dir, str(cache_dir)
                )


class TestTerraformHelpersCreateMirrorConfiguration:
    def test_create_mirror_configuration(self, providers_collection):
        includes = ["provider1", "provider2"]
        tf_config = _create_mirror_configuration(providers_collection, includes)
        assert "terraform {" in tf_config


class TestTerraformHelpersParseRequiredProviders:
    def test_parse_required_providers(self):
        content = {
            "terraform": [
                {
                    "required_providers": [
                        {
                            "provider1": {
                                "source": "hashicorp/provider1",
                                "version": "1.0.0",
                            }
                        }
                    ]
                }
            ]
        }
        expected_providers = {
            "provider1": {"source": "hashicorp/provider1", "version": "1.0.0"}
        }
        assert _parse_required_providers(content) == expected_providers

    def test_parse_required_providers_no_providers(self):
        content = {"terraform": [{"required_providers": []}]}
        assert _parse_required_providers(content) == {}

    def test_parse_required_providers_no_terraform(self):
        content = {
            "required_providers": [
                {"provider1": {"source": "hashicorp/provider1", "version": "1.0.0"}}
            ]
        }
        assert _parse_required_providers(content) == {}


class TestTerraformHelpersFindRequiredProviders:

    def test_find_required_providers(self, tmp_path):
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
            "provider1": {
                "source": "hashicorp/provider1",
                "version": SpecifierSet("==1.0.0"),
            }
        }
        assert providers == expected_providers

    def test_find_required_providers_multiple_versions(self, tmp_path):
        tf_content_a = """
        terraform {
            required_providers {
                provider1 = {
                source = "hashicorp/provider1"
                version = ">=1.0.0"
                }
            }
        }
        """
        tf_content_b = """
        terraform {
            required_providers {
                provider1 = {
                source = "hashicorp/provider1"
                version = "==1.5.0"
                }
            }
        }
        """
        test_file_a = tmp_path / "main.tf"
        test_file_b = tmp_path / "main2.tf"
        with open(test_file_a, "w") as f:
            f.write(tf_content_a)
        with open(test_file_b, "w") as f:
            f.write(tf_content_b)
        providers = _find_required_providers(str(tmp_path))
        expected_providers = {
            "provider1": {
                "source": "hashicorp/provider1",
                "version": (SpecifierSet(">=1.0.0") & SpecifierSet("==1.5.0")),
            }
        }
        assert providers == expected_providers

    def test_find_required_providers_invalid_version(self, tmp_path):
        tf_content = """
        terraform {
            required_providers {
                provider1 = {
                source = "hashicorp/provider1"
                version = ">=@~~1.0.0"
                }
            }
        }
        """
        test_file = tmp_path / "main.tf"
        with open(test_file, "w") as f:
            f.write(tf_content)
        with pytest.raises(TFWorkerException):
            _find_required_providers(str(tmp_path))

    def test_find_required_providers_no_providers(self, tmp_path):
        tf_content = """
        terraform {
        }
        """
        test_file = tmp_path / "main.tf"
        with open(test_file, "w") as f:
            f.write(tf_content)

        providers = _find_required_providers(str(tmp_path))
        assert providers == {}

    def test_find_required_providers_invalid_hcl(self, tmp_path):
        tf_content = """
        tfworker { izgreat! }
        """
        test_file = tmp_path / "main.tf"
        with open(test_file, "w") as f:
            f.write(tf_content)

        providers = _find_required_providers(str(tmp_path))
        assert providers == {}

    def test_find_providers_conflicting_source(self, tmp_path):
        tf_content_a = """
        terraform {
            required_providers {
                provider1 = {
                source = "hashicorp/provider1"
                version = ">=1.0.0"
                }
            }
        }
        """
        tf_content_b = """
        terraform {
            required_providers {
                provider1 = {
                source = "hashicorp/provider2"
                version = "==1.5.0"
                }
            }
        }
        """
        test_file_a = tmp_path / "main.tf"
        test_file_b = tmp_path / "main2.tf"
        with open(test_file_a, "w") as f:
            f.write(tf_content_a)
        with open(test_file_b, "w") as f:
            f.write(tf_content_b)
        with pytest.raises(TFWorkerException):
            _find_required_providers(str(tmp_path))


class TestTerraformHelpersPessimisticOperator:
    def test_get_specifier_set_pessimistic_major_only(self):
        # '~> 1' means '>=1, <2'
        assert _get_specifier_set("~> 1") == SpecifierSet(">=1,<2")

    def test_get_specifier_set_pessimistic_major_minor(self):
        # '~> 3.0' means '>=3.0, <4'
        assert _get_specifier_set("~> 3.0") == SpecifierSet(">=3.0,<4")

    def test_get_specifier_set_pessimistic_full(self):
        # '~> 1.2.3' means '>=1.2.3, <1.3.0'
        assert _get_specifier_set("~> 1.2.3") == SpecifierSet(">=1.2.3,<1.3.0")

    def test_get_specifier_set_pessimistic_with_additional_constraints(self):
        # Combined constraints are preserved and expanded
        s = _get_specifier_set("~> 1.2.3, != 1.2.5")
        assert s == SpecifierSet(">=1.2.3,<1.3.0,!=1.2.5")

    def test_find_required_providers_with_pessimistic(self, tmp_path):
        # Ensure HCL parsing and specifier expansion works end-to-end
        tf_content = """
        terraform {
            required_providers {
                aws = {
                    source = "hashicorp/aws"
                    version = "~> 4.10.0"
                }
            }
        }
        """
        test_file = tmp_path / "main.tf"
        with open(test_file, "w") as f:
            f.write(tf_content)

        providers = _find_required_providers(str(tmp_path))
        assert providers == {
            "aws": {
                "source": "hashicorp/aws",
                "version": SpecifierSet(">=4.10.0,<4.11.0"),
            }
        }


class TestSpecifierToTerraform:
    def test_specifier_to_terraform_from_specset_equality(self):
        s = SpecifierSet("==1.2.3")
        assert specifier_to_terraform(s) == "=1.2.3"

    def test_specifier_to_terraform_from_specset_range(self):
        s = SpecifierSet(">=1.2.3,<1.3.0")
        out = specifier_to_terraform(s)
        assert set(out.split(",")) == set(">=1.2.3,<1.3.0".split(","))

    def test_specifier_to_terraform_from_string_terraform_passthrough(self):
        # Should preserve Terraform's '~>' operator when passed as a string
        assert specifier_to_terraform("~> 2.4.0") == "~> 2.4.0"

    def test_specifier_to_terraform_from_string_equality(self):
        assert specifier_to_terraform("==1.0.0") == "=1.0.0"


class TestLockfileConcreteVersionValidation:
    def test_generate_lockfile_raises_on_constraint_version(self, tmp_path):
        # Build a ProvidersCollection with a constraint instead of a single version
        providers_odict = {
            "provider1": {
                "requirements": {
                    "source": "hashicorp/provider1",
                    "version": ">=1.0.0",
                }
            },
        }
        providers = ProvidersCollection(
            providers_odict=providers_odict, authenticators=MagicMock()
        )

        with pytest.raises(TFWorkerException):
            generate_terraform_lockfile(
                providers=providers,
                included_providers=None,
                cache_dir=str(tmp_path),
            )


def _required(name, source):
    return f"""
terraform {{
  required_providers {{
    {name} = {{
      source  = "{source}"
      version = ">= 1.0.0"
    }}
  }}
}}
"""


class TestTerraformHelpersFindLoadedRequiredProviders:

    def _write_manifest(self, root, modules):
        manifest = root / ".terraform" / "modules" / "modules.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"Modules": [{"Key": "", "Source": "", "Dir": "."}] + modules})
        )

    def test_subdir_source_reads_only_the_subdir(self, tmp_path):
        (tmp_path / "main.tf").write_text(_required("aws", "hashicorp/aws"))
        # a //sub git source fetches the whole repository
        repo = tmp_path / ".terraform" / "modules" / "shared"
        (repo / "sub").mkdir(parents=True)
        (repo / "versions.tf").write_text(_required("archive", "hashicorp/archive"))
        (repo / "sub" / "versions.tf").write_text(_required("tls", "hashicorp/tls"))
        self._write_manifest(
            tmp_path,
            [
                {
                    "Key": "shared",
                    "Source": "git::x//sub",
                    "Dir": ".terraform/modules/shared/sub",
                }
            ],
        )

        providers = _find_loaded_required_providers(str(tmp_path))
        assert sorted(providers) == ["aws", "tls"]

    def test_local_module_is_read(self, tmp_path):
        (tmp_path / "local").mkdir()
        (tmp_path / "local" / "versions.tf").write_text(
            _required("random", "hashicorp/random")
        )
        (tmp_path / "unused").mkdir()
        (tmp_path / "unused" / "versions.tf").write_text(
            _required("tls", "hashicorp/tls")
        )
        self._write_manifest(
            tmp_path, [{"Key": "local", "Source": "./local", "Dir": "local"}]
        )

        providers = _find_loaded_required_providers(str(tmp_path))
        assert sorted(providers) == ["random"]

    def test_provider_blocks_are_required(self, tmp_path):
        (tmp_path / "worker_generated_terraform.tf").write_text(
            'provider "aws" {\n  region = "us-east-1"\n}\n'
            'provider "aws" {\n  alias = "use2"\n  region = "us-east-2"\n}\n'
        )
        self._write_manifest(tmp_path, [])

        providers = _find_loaded_required_providers(str(tmp_path))
        assert sorted(providers) == ["aws"]

    def test_provider_block_read_before_required_providers(self, tmp_path):
        # providers.tf sorts before versions.tf, so the block is read first
        (tmp_path / "providers.tf").write_text('provider "aws" {\n  region = "x"\n}\n')
        (tmp_path / "versions.tf").write_text(_required("aws", "hashicorp/aws"))
        self._write_manifest(tmp_path, [])

        providers = _find_loaded_required_providers(str(tmp_path))
        assert providers["aws"]["source"] == "hashicorp/aws"

    def test_generated_provider_block_and_module_requirement(self, tmp_path):
        (tmp_path / "worker_generated_terraform.tf").write_text('provider "tls" {}\n')
        (tmp_path / "local").mkdir()
        (tmp_path / "local" / "versions.tf").write_text(
            _required("tls", "hashicorp/tls")
        )
        self._write_manifest(
            tmp_path, [{"Key": "local", "Source": "./local", "Dir": "local"}]
        )

        providers = _find_loaded_required_providers(str(tmp_path))
        assert providers["tls"]["source"] == "hashicorp/tls"

    def test_provider_block_only_has_no_source(self, tmp_path):
        (tmp_path / "providers.tf").write_text('provider "tls" {}\n')
        self._write_manifest(tmp_path, [])

        providers = _find_loaded_required_providers(str(tmp_path))
        assert providers == {"tls": {"source": "", "version": SpecifierSet("")}}

    @pytest.mark.parametrize(
        "manifest",
        ["{not json", "[]", '{"Modules": {}}', '{"Modules": ["x"]}'],
    )
    def test_malformed_manifest_raises(self, tmp_path, manifest):
        path = tmp_path / ".terraform" / "modules" / "modules.json"
        path.parent.mkdir(parents=True)
        path.write_text(manifest)

        with pytest.raises(TFWorkerException):
            _find_loaded_required_providers(str(tmp_path))

    def test_missing_module_dir_is_skipped(self, tmp_path):
        (tmp_path / "main.tf").write_text(_required("aws", "hashicorp/aws"))
        self._write_manifest(
            tmp_path, [{"Key": "gone", "Source": "./gone", "Dir": "gone"}]
        )

        providers = _find_loaded_required_providers(str(tmp_path))
        assert sorted(providers) == ["aws"]

    def test_falls_back_to_walking_without_a_manifest(self, tmp_path):
        (tmp_path / "main.tf").write_text(_required("aws", "hashicorp/aws"))
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "versions.tf").write_text(_required("tls", "hashicorp/tls"))

        providers = _find_loaded_required_providers(str(tmp_path))
        assert providers == _find_required_providers(str(tmp_path))
        assert sorted(providers) == ["aws", "tls"]
