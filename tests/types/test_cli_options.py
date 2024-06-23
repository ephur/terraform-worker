# from pathlib import Path
# from tempfile import NamedTemporaryFile, TemporaryDirectory

# import pytest

# from tfworker.types import CLIOptionsRoot


# def test_working_dir_validator_with_valid_directory():
#     with TemporaryDirectory() as temp_dir:
#         options = CLIOptionsRoot(working_dir=temp_dir)
#         assert options.working_dir == temp_dir


# def test_working_dir_validator_with_non_existent_directory():
#     with pytest.raises(ValueError, match=r"Working path .* does not exist!"):
#         CLIOptionsRoot(working_dir="/non/existent/path")


# def test_working_dir_validator_with_file_instead_of_directory():
#     with TemporaryDirectory() as temp_dir:
#         file_path = Path(temp_dir) / "file.txt"
#         file_path.touch()
#         with pytest.raises(ValueError, match=r"Working path .* is not a directory!"):
#             CLIOptionsRoot(working_dir=str(file_path))


# def test_working_dir_validator_with_non_empty_directory():
#     with TemporaryDirectory() as temp_dir:
#         (Path(temp_dir) / "file.txt").touch()
#         with pytest.raises(ValueError, match=r"Working path .* must be empty!"):
#             CLIOptionsRoot(working_dir=temp_dir)


# def test_clean_validator_with_working_dir_set_and_clean_not_set():
#     with TemporaryDirectory() as temp_dir:
#         options = CLIOptionsRoot(working_dir=temp_dir)
#         assert options.clean is False


# def test_clean_validator_with_working_dir_not_set_and_clean_not_set():
#     options = CLIOptionsRoot()
#     assert options.clean is True


# def test_clean_validator_with_working_dir_set_and_clean_set_to_true():
#     with TemporaryDirectory() as temp_dir:
#         options = CLIOptionsRoot(working_dir=temp_dir, clean=True)
#         assert options.clean is True


# def test_clean_validator_with_working_dir_not_set_and_clean_set_to_false():
#     options = CLIOptionsRoot(clean=False)
#     assert options.clean is False


# def test_validate_gcp_creds_path():
#     # Test with a non-existing file
#     with pytest.raises(ValueError, match=r"Path .* is not a file!"):
#         CLIOptionsRoot(gcp_creds_path="non_existing_file.json")

#     # Test with a directory
#     with pytest.raises(ValueError, match=r"Path .* is not a file!"):
#         CLIOptionsRoot(gcp_creds_path=".")

#     # Test with a valid file
#     # Create a temporary file for the test
#     with NamedTemporaryFile() as temp_file:
#         # The validator should not raise any exception for a valid file
#         CLIOptionsRoot(gcp_creds_path=temp_file.name)
