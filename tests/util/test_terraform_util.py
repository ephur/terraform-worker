import shutil

import pytest

from tfworker.util.terraform import prep_modules


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

    # @pytest.mark.parametrize(
    #     "stdout, major, minor, expected_exception",
    #     [
    #         ("Terraform v0.12.29", 0, 12, does_not_raise()),
    #         ("Terraform v1.3.5", 1, 3, does_not_raise()),
    #         ("TF 14", "", "", pytest.raises(SystemExit)),
    #     ],
    # )
    # def test_get_tf_version(
    #     self, stdout: str, major: int, minor: int, expected_exception: callable
    # ):
    #     with mock.patch(
    #         "tfworker.commands.base.pipe_exec",
    #         side_effect=mock_tf_version,
    #     ) as mocked:
    #         with expected_exception:
    #             (actual_major, actual_minor) = BaseCommand.get_terraform_version(stdout)
    #             assert actual_major == major
    #             assert actual_minor == minor
    #             mocked.assert_called_once()
