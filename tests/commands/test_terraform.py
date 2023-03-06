# Copyright 2020 Richard Maynard (richard.maynard@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import filecmp
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Tuple
from unittest import mock

import pytest
from google.cloud.exceptions import NotFound
from pytest_lazyfixture import lazy_fixture

import tfworker
from tfworker.backends.base import BackendError
from tfworker.commands.terraform import BaseCommand


# context manager to allow testing exceptions in parameterized tests
@contextmanager
def does_not_raise():
    yield


def mock_pipe_exec(
    args: str,
    stdin: str = None,
    cwd: str = None,
    env: list = None,
    stream_output: bool = False,
):
    return (0, "".encode(), "".encode())


def mock_tf_version(args: str) -> Tuple[int, str, str]:
    return (0, args.encode(), "".encode())


class TestTerraformCommand:
    @pytest.mark.parametrize("tf_cmd", [lazy_fixture("tf_12cmd")])
    def test_prep_modules(self, tf_cmd, rootc):
        tf_cmd.prep_modules()
        for test_file in [
            "/terraform-modules/test_a/test.tf",
            "/terraform-modules/test_b/test.tf",
        ]:
            src = rootc.args.repository_path + test_file
            dst = rootc.temp_dir + test_file
            assert filecmp.cmp(src, dst, shallow=False)

    @pytest.mark.parametrize("tf_cmd", [lazy_fixture("tf_12cmd")])
    def test_terraform_modules_dir(self, tf_cmd, rootc):
        with tempfile.TemporaryDirectory() as d:
            test_files = [Path("test_a/test.tf"), Path("test_b/test.tf")]
            for f in test_files:
                testfile = Path(f"{d}/{f}")
                sourcefile = Path(f"tests/fixtures/terraform-modules/{f}")
                parent = testfile.parent
                parent.mkdir(parents=True)
                testfile.write_bytes(sourcefile.read_bytes())

            tf_cmd._terraform_modules_dir = d
            tf_cmd.prep_modules()
            for test_file in [
                "/terraform-modules/test_a/test.tf",
                "/terraform-modules/test_b/test.tf",
            ]:
                src = rootc.args.repository_path + test_file
                dst = rootc.temp_dir + test_file
                assert filecmp.cmp(src, dst, shallow=False)

    @pytest.mark.parametrize(
        "method, tf_cmd, args",
        [
            (
                "init",
                lazy_fixture("tf_12cmd"),
                ["-input=false", "-no-color", "-plugin-dir"],
            ),
            (
                "plan",
                lazy_fixture("tf_12cmd"),
                ["-input=false", "-detailed-exitcode", "-no-color"],
            ),
            (
                "apply",
                lazy_fixture("tf_12cmd"),
                ["-input=false", "-no-color", "-auto-approve"],
            ),
            (
                "destroy",
                lazy_fixture("tf_12cmd"),
                ["-input=false", "-no-color", "-auto-approve"],
            ),
            (
                "init",
                lazy_fixture("tf_13cmd"),
                ["-input=false", "-no-color", "-plugin-dir"],
            ),
            (
                "plan",
                lazy_fixture("tf_13cmd"),
                ["-input=false", "-detailed-exitcode", "-no-color"],
            ),
            (
                "apply",
                lazy_fixture("tf_13cmd"),
                ["-input=false", "-no-color", "-auto-approve"],
            ),
            (
                "destroy",
                lazy_fixture("tf_13cmd"),
                ["-input=false", "-no-color", "-auto-approve"],
            ),
            (
                "init",
                lazy_fixture("tf_14cmd"),
                ["-input=false", "-no-color", "-plugin-dir"],
            ),
            (
                "plan",
                lazy_fixture("tf_14cmd"),
                ["-input=false", "-detailed-exitcode", "-no-color"],
            ),
            (
                "apply",
                lazy_fixture("tf_14cmd"),
                ["-input=false", "-no-color", "-auto-approve"],
            ),
            (
                "destroy",
                lazy_fixture("tf_14cmd"),
                ["-input=false", "-no-color", "-auto-approve"],
            ),
            (
                "init",
                lazy_fixture("tf_15cmd"),
                ["-input=false", "-no-color", "-plugin-dir"],
            ),
            (
                "plan",
                lazy_fixture("tf_15cmd"),
                ["-input=false", "-detailed-exitcode", "-no-color"],
            ),
            (
                "apply",
                lazy_fixture("tf_15cmd"),
                ["-input=false", "-no-color", "-auto-approve"],
            ),
            (
                "destroy",
                lazy_fixture("tf_15cmd"),
                ["-input=false", "-no-color", "-auto-approve"],
            ),
        ],
    )
    def test_run(self, tf_cmd: str, method: callable, args: list):
        with mock.patch(
            "tfworker.commands.terraform.pipe_exec",
            side_effect=mock_pipe_exec,
        ) as mocked:
            tf_cmd._run(
                tf_cmd.definitions["test"],
                method,
            )
            mocked.assert_called_once()
            call_as_string = str(mocked.mock_calls.pop())
            assert method in call_as_string
            for arg in args:
                assert arg in call_as_string

    @pytest.mark.parametrize(
        "stdout, major, minor, expected_exception",
        [
            ("Terraform v0.12.29", 0, 12, does_not_raise()),
            ("Terraform v1.3.5", 1, 3, does_not_raise()),
            ("TF 14", "", "", pytest.raises(SystemExit)),
        ],
    )
    def test_get_tf_version(
        self, stdout: str, major: int, minor: int, expected_exception: callable
    ):
        with mock.patch(
            "tfworker.commands.base.pipe_exec",
            side_effect=mock_tf_version,
        ) as mocked:
            with expected_exception:
                (actual_major, actual_minor) = BaseCommand.get_terraform_version(stdout)
                assert actual_major == major
                assert actual_minor == minor
                mocked.assert_called_once()

    def test_worker_options(self, tf_13cmd_options):
        # Verify that the options from the CLI override the options from the config
        assert tf_13cmd_options._rootc.worker_options_odict.get("backend") == "s3"
        assert tf_13cmd_options.backend.tag == "gcs"

        # Verify that None options are overriden by the config
        assert tf_13cmd_options._rootc.worker_options_odict.get("b64_encode") is True
        assert tf_13cmd_options._args_dict.get("b64_encode") is False

        # The fixture causes which to return /usr/local/bin/terraform.  However, since the
        # path is specified in the worker_options, assert the value fromt he config.
        assert tf_13cmd_options._terraform_bin == "/home/test/bin/terraform"

    # def test_no_create_backend_bucket_fails_s3(self, rootc_no_create_backend_bucket):
    #     with pytest.raises(BackendError):
    #         with mock.patch(
    #             "tfworker.commands.base.BaseCommand.get_terraform_version",
    #             side_effect=lambda x: (13, 3),
    #         ):
    #             with mock.patch(
    #                 "tfworker.commands.base.which",
    #                 side_effect=lambda x: "/usr/local/bin/terraform",
    #             ):
    #                 return tfworker.commands.base.BaseCommand(
    #                     rootc_no_create_backend_bucket, "test-0001", tf_version_major=13
    #                 )

    def test_no_create_backend_bucket_fails_gcs(self, grootc_no_create_backend_bucket):
        with pytest.raises(BackendError):
            with mock.patch(
                "tfworker.commands.base.BaseCommand.get_terraform_version",
                side_effect=lambda x: (13, 3),
            ):
                with mock.patch(
                    "tfworker.commands.base.which",
                    side_effect=lambda x: "/usr/local/bin/terraform",
                ):
                    with mock.patch(
                        "tfworker.backends.gcs.storage.Client.from_service_account_json"
                    ) as ClientMock:
                        instance = ClientMock.return_value
                        instance.get_bucket.side_effect = NotFound("bucket not found")
                        return tfworker.commands.base.BaseCommand(
                            grootc_no_create_backend_bucket,
                            "test-0001",
                            tf_version_major=13,
                        )
