import shutil
from contextlib import contextmanager
from unittest.mock import MagicMock, call, patch

import pytest

from tfworker.constants import (
    DEFAULT_REPOSITORY_PATH,
    TF_PROVIDER_DEFAULT_HOSTNAME,
    TF_PROVIDER_DEFAULT_NAMESPACE,
)
from tfworker.providers.providers_collection import ProvidersCollection
from tfworker.types import ProviderGID
from tfworker.util.terraform import (
    find_required_providers,
    generate_terraform_lockfile,
    get_provider_gid_from_source,
    get_terraform_version,
    mirror_providers,
    prep_modules,
)


@contextmanager
def does_not_raise():
    yield


@pytest.fixture
def providers_collection():
    providers_odict = {
        "provider1": {
            "requirements": {"source": "hashicorp/provider1", "version": "1.0.0"}
        },
        "provider2": {
            "requirements": {"source": "hashicorp/provider2", "version": "2.0.0"}
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


def test_prep_modules(tmp_path):
    test_file_content = "test"

    module_path = tmp_path / "terraform-modules"
    module_path.mkdir()

    target_path = tmp_path / "target"
    target_path.mkdir()

    # Create a test module directory with a file
    test_module_dir = module_path / "test_module_dir"
    test_module_dir.mkdir()
    test_module_file = test_module_dir / "test_module_file.tf"
    with open(test_module_file, "w") as f:
        f.write(test_file_content)
    test_module_ignored_file = test_module_dir / "test_module_ignored_file.txt"
    test_module_ignored_file.touch()
    test_module_default_ignored_file = test_module_dir / "terraform.tfstate"
    test_module_default_ignored_file.touch()

    prep_modules(str(module_path), str(target_path))

    final_target_path = target_path / "terraform-modules" / "test_module_dir"

    # check the target path exists
    assert final_target_path.exists()

    # check the file is copied to the target directory
    assert (final_target_path / "test_module_file.tf").exists()

    # check the file content is the same
    with open(final_target_path / "test_module_file.tf") as f:
        assert f.read() == test_file_content

    # check that the ignored file is not copied to the target directory
    assert not (final_target_path / "terraform.tfstate").exists()

    # remove the contents of the target directory
    shutil.rmtree(target_path)
    assert not target_path.exists()

    # Use a custom ignore pattern
    prep_modules(str(module_path), str(target_path), ignore_patterns=["*.txt"])

    # ensure the default ignored file is copied
    assert (final_target_path / "terraform.tfstate").exists()

    # ensure the custom ignored file is not copied
    assert not (final_target_path / "test_module_ignored_file.txt").exists()


def test_prep_modules_not_found(tmp_path):
    module_path = tmp_path / "terraform-modules"
    target_path = tmp_path / "target"

    prep_modules(str(module_path), str(target_path))

    # check the target path does not exist
    assert not target_path.exists()


def test_prep_modules_required(tmp_path):
    module_path = tmp_path / "terraform-modules"
    target_path = tmp_path / "target"

    with pytest.raises(SystemExit):
        prep_modules(str(module_path), str(target_path), required=True)

    # check the target path does not exist
    assert not target_path.exists()


def test_prep_modules_default_path():
    class MockPath:
        def __init__(self, exists_return_value):
            self.exists_return_value = exists_return_value

        def exists(self):
            return self.exists_return_value

    with patch(
        "pathlib.Path", return_value=MockPath(exists_return_value=False)
    ) as MockPath:
        result = prep_modules("", "test_target")
        assert result is None
        assert MockPath.call_count == 2
        MockPath.assert_has_calls(
            [
                call(f"{DEFAULT_REPOSITORY_PATH}/terraform-modules"),
                call("test_target/terraform-modules"),
            ],
            any_order=True,
        )


@pytest.mark.parametrize(
    "stdout, stderr, return_code, major, minor, expected_exception",
    [
        ("Terraform v0.12.29", "", 0, 0, 12, does_not_raise()),
        ("Terraform v1.3.5", "", 0, 1, 3, does_not_raise()),
        ("TF 14", "", 0, "", "", pytest.raises(SystemExit)),
        ("", "error", 1, "", "", pytest.raises(SystemExit)),
    ],
)
def test_get_tf_version(
    stdout: str,
    stderr: str,
    return_code: int,
    major: int,
    minor: int,
    expected_exception: callable,
):
    with patch(
        "tfworker.util.terraform.pipe_exec",
        side_effect=[(return_code, stdout.encode(), stderr.encode())],
    ) as mocked:
        with expected_exception:
            (actual_major, actual_minor) = get_terraform_version(stdout)
            assert actual_major == major
            assert actual_minor == minor
            mocked.assert_called_once()


@pytest.fixture
def mock_mirror_setup():
    mock_mirror_settings = {
        "providers": MagicMock(),
        'terraform_bin': "/path/to/terraform",
        "working_dir": "/working/dir",
        "cache_dir": "/cache/dir",
        "temp_dir": "/temp/dir",
    }
    with patch("tfworker.util.terraform.pipe_exec") as mock_pipe_exec, patch(
        "tfworker.util.terraform.tfhelpers._write_mirror_configuration"
    ) as mock_write_mirror_configuration, patch(
        "tfworker.util.terraform.tfhelpers._validate_cache_dir"
    ) as mock_validate_cache_dir, patch(
        "tfworker.util.terraform.click.secho"
    ) as mock_secho:

        yield mock_secho, mock_validate_cache_dir, mock_write_mirror_configuration, mock_pipe_exec, mock_mirror_settings


def test_mirror_providers(mock_mirror_setup):
    (
        mock_secho,
        mock_validate_cache_dir,
        mock_write_mirror_configuration,
        mock_pipe_exec,
        mock_mirror_settings,
    ) = mock_mirror_setup
    mock_write_mirror_configuration.return_value.__enter__.return_value = (
        mock_mirror_settings["temp_dir"]
    )
    mock_pipe_exec.return_value = (0, b"stdout", b"stderr")

    result = mirror_providers(
        providers=mock_mirror_settings["providers"],
        terraform_bin=mock_mirror_settings['terraform_bin'],
        working_dir=mock_mirror_settings["working_dir"],
        cache_dir=mock_mirror_settings["cache_dir"],
    )

    mock_validate_cache_dir.assert_called_once_with(mock_mirror_settings["cache_dir"])
    mock_write_mirror_configuration.assert_called_once_with(
        mock_mirror_settings["providers"],
        mock_mirror_settings["working_dir"],
        mock_mirror_settings["cache_dir"],
    )
    mock_pipe_exec.assert_called_once_with(
        f"{mock_mirror_settings['terraform_bin']} providers mirror {mock_mirror_settings['cache_dir']}",
        cwd=mock_mirror_settings["temp_dir"],
        stream_output=True,
    )
    assert result is None


def test_mirror_providers_tf_error(mock_mirror_setup):
    (
        mock_secho,
        mock_validate_cache_dir,
        mock_write_mirror_configuration,
        mock_pipe_exec,
        mock_mirror_settings,
    ) = mock_mirror_setup
    mock_write_mirror_configuration.return_value.__enter__.return_value = (
        mock_mirror_settings["temp_dir"]
    )
    mock_pipe_exec.return_value = (1, b"stdout", b"stderr")

    with pytest.raises(SystemExit):
        mirror_providers(
            providers=mock_mirror_settings["providers"],
            terraform_bin=mock_mirror_settings['terraform_bin'],
            working_dir=mock_mirror_settings["working_dir"],
            cache_dir=mock_mirror_settings["cache_dir"],
        )

    mock_validate_cache_dir.assert_called_once_with(mock_mirror_settings["cache_dir"])
    mock_write_mirror_configuration.assert_called_once_with(
        mock_mirror_settings["providers"],
        mock_mirror_settings["working_dir"],
        mock_mirror_settings["cache_dir"],
    )
    mock_pipe_exec.assert_called_once_with(
        f"{mock_mirror_settings['terraform_bin']} providers mirror {mock_mirror_settings['cache_dir']}",
        cwd=mock_mirror_settings["temp_dir"],
        stream_output=True,
    )


def test_mirror_providers_all_in_cache(mock_mirror_setup):
    (
        mock_secho,
        mock_validate_cache_dir,
        mock_write_mirror_configuration,
        mock_pipe_exec,
        mock_mirror_settings,
    ) = mock_mirror_setup
    mock_write_mirror_configuration.return_value.__enter__.side_effect = IndexError

    mirror_providers(
        providers=mock_mirror_settings["providers"],
        terraform_bin=mock_mirror_settings['terraform_bin'],
        working_dir=mock_mirror_settings["working_dir"],
        cache_dir=mock_mirror_settings["cache_dir"],
    )

    mock_validate_cache_dir.assert_called_once_with(mock_mirror_settings["cache_dir"])
    mock_write_mirror_configuration.assert_called_once_with(
        mock_mirror_settings["providers"],
        mock_mirror_settings["working_dir"],
        mock_mirror_settings["cache_dir"],
    )
    mock_pipe_exec.assert_not_called()
    mock_secho.assert_called_with("All providers in cache", fg="yellow")


@patch("tfworker.util.terraform.click.secho")
@patch("tfworker.util.terraform.tfhelpers._get_cached_hash")
@patch("tfworker.util.terraform.tfhelpers._not_in_cache")
def test_generate_terraform_lockfile(
    mock_not_in_cache, mock_get_cached_hash, mock_secho, providers_collection
):
    providers = providers_collection
    included_providers = ["provider1"]
    cache_dir = "/cache/dir"
    mock_not_in_cache.return_value = False
    mock_get_cached_hash.return_value = ["hash1", "hash2"]

    expected_result = """provider "registry.terraform.io/hashicorp/provider1" {
  version     = "1.0.0"
  constraints = "1.0.0"
  hashes = [
    "hash1",
    "hash2",
  ]
}
"""

    result = generate_terraform_lockfile(providers, included_providers, cache_dir)
    mock_not_in_cache.assert_called()
    mock_get_cached_hash.assert_called()
    assert result == expected_result


@patch("tfworker.util.terraform.click.secho")
@patch("tfworker.util.terraform.tfhelpers._get_cached_hash")
@patch("tfworker.util.terraform.tfhelpers._not_in_cache")
def test_generate_terraform_lockfile_no_includes(
    mock_not_in_cache, mock_get_cached_hash, mock_secho, providers_collection
):
    providers = providers_collection
    included_providers = None
    cache_dir = "/cache/dir"
    mock_not_in_cache.return_value = False
    mock_get_cached_hash.return_value = ["hash1", "hash2"]

    expected_result = """provider "registry.terraform.io/hashicorp/provider1" {
  version     = "1.0.0"
  constraints = "1.0.0"
  hashes = [
    "hash1",
    "hash2",
  ]
}

provider "registry.terraform.io/hashicorp/provider2" {
  version     = "2.0.0"
  constraints = "2.0.0"
  hashes = [
    "hash1",
    "hash2",
  ]
}
"""

    result = generate_terraform_lockfile(providers, included_providers, cache_dir)
    mock_not_in_cache.assert_called()
    mock_get_cached_hash.assert_called()
    assert result == expected_result


@patch("tfworker.util.terraform.click.secho")
@patch("tfworker.util.terraform.tfhelpers._get_cached_hash")
@patch("tfworker.util.terraform.tfhelpers._not_in_cache")
def test_generate_terraform_lockfile_not_in_cache(
    mock_not_in_cache, mock_get_cached_hash, mock_secho
):
    providers = MagicMock()
    providers.__iter__.return_value = [MagicMock()]
    included_providers = ["provider1", "provider2"]
    cache_dir = "/cache/dir"
    mock_not_in_cache.return_value = True

    result = generate_terraform_lockfile(providers, included_providers, cache_dir)

    mock_secho.assert_called_once_with(
        f"Generating lockfile for providers: {included_providers}", fg="yellow"
    )
    mock_not_in_cache.assert_called()
    assert result is None


def test_get_provider_gid_from_source_full():
    result = get_provider_gid_from_source("example.com/namespace/provider")
    assert result == ProviderGID(
        hostname="example.com", namespace="namespace", type="provider"
    )


def test_get_provider_gid_from_source_long():
    with pytest.raises(ValueError):
        get_provider_gid_from_source("example.com/namespace/provider/invalid")


def test_get_provider_gid_from_source_short():
    with pytest.raises(ValueError):
        get_provider_gid_from_source(None)


def test_get_provider_from_source_provider():
    result = get_provider_gid_from_source("provider")
    assert result == ProviderGID(
        hostname=TF_PROVIDER_DEFAULT_HOSTNAME,
        namespace=TF_PROVIDER_DEFAULT_NAMESPACE,
        type="provider",
    )


def test_get_provider_from_source_namespace():
    result = get_provider_gid_from_source("namespace/provider")
    assert result == ProviderGID(
        hostname=TF_PROVIDER_DEFAULT_HOSTNAME, namespace="namespace", type="provider"
    )


@patch("tfworker.util.terraform.tfhelpers._find_required_providers")
def test_find_required_providers(mock_find_required_providers):
    search_dir = "/search/dir"
    mock_find_required_providers.return_value = {
        "provider1": [{"version": "1.0.0", "source": "hashicorp/provider1"}]
    }

    result = find_required_providers(search_dir)

    mock_find_required_providers.assert_called_once_with(search_dir)
    assert result == {
        "provider1": [{"version": "1.0.0", "source": "hashicorp/provider1"}]
    }


@patch("tfworker.util.terraform.tfhelpers._find_required_providers")
def test_find_required_providers_empty(mock_find_required_providers):
    search_dir = "/search/dir/empty"
    mock_find_required_providers.return_value = {}

    result = find_required_providers(search_dir)

    mock_find_required_providers.assert_called_once_with(search_dir)
    assert result is None
