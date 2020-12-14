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
from contextlib import contextmanager
from unittest import mock

import pytest
from pytest_lazyfixture import lazy_fixture
from tfworker.commands.terraform import TerraformCommand

# context manager to allow testing exceptions in parameterized tests
@contextmanager
def does_not_raise():
    yield


def mock_pipe_exec(args, stdin=None, cwd=None, env=None):
    return (0, "".encode(), "".encode())

def mock_tf_version(args):
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

    @pytest.mark.parametrize(
        "method, tf_cmd",
        [
            ("init", lazy_fixture("tf_12cmd")),
            ("plan", lazy_fixture("tf_12cmd")),
            ("apply", lazy_fixture("tf_12cmd")),
            ("init", lazy_fixture("tf_13cmd")),
            ("plan", lazy_fixture("tf_13cmd")),
            ("apply", lazy_fixture("tf_13cmd")),
        ],
    )
    def test_run(self, tf_cmd, method):
        with mock.patch(
            "tfworker.commands.terraform.TerraformCommand.pipe_exec",
            side_effect=mock_pipe_exec,
        ) as mocked:
            tf_cmd._run(
                tf_cmd.definitions["test"],
                method,
            )
            mocked.assert_called_once()

    @pytest.mark.parametrize(
        "commands, exit_code, cwd, stdin, stdout, stderr",
        [
            ("/usr/bin/env true", 0, None, None, "", ""),
            ("/usr/bin/env false", 1, None, None, "", ""),
            ("/bin/echo foo", 0, None, None, "foo", ""),
            ("/usr/bin/env grep foo", 0, None, "foo", "foo", ""),
            ("/bin/pwd", 0, "/tmp", None, "/tmp", ""),
            (
                "/bin/cat /yisohwo0AhK8Ah ",
                1,
                None,
                None,
                "",
                "/bin/cat: /yisohwo0AhK8Ah: No such file or directory",
            ),
            (["/bin/echo foo", "/usr/bin/env grep foo"], 0, None, None, "foo", ""),
            (["/bin/echo foo", "/usr/bin/env grep bar"], 1, None, None, "", ""),
        ],
    )
    def test_run_pipe_exec(self, commands, exit_code, cwd, stdin, stdout, stderr):
        (return_exit_code, return_stdout, return_stderr) = TerraformCommand.pipe_exec(
            commands, cwd=cwd, stdin=stdin
        )

        assert return_exit_code == exit_code
        assert stdout.encode() in return_stdout.rstrip()
        assert return_stderr.rstrip() in stderr.encode()

    @pytest.mark.parametrize(
        "stdout, major, minor, expected_exception",
        [
            ("Terraform v0.12.29", 12, 29, does_not_raise()),
            ("Terraform v0.13.5", 13, 5, does_not_raise()),
            ("TF 14", "", "", pytest.raises(SystemExit))
        ]
    )
    def test_get_tf_version(self, stdout, major, minor, expected_exception):
        with mock.patch(
            "tfworker.commands.terraform.TerraformCommand.pipe_exec",
            side_effect=mock_tf_version
        ) as mocked:
            with expected_exception:
                (actual_major, actual_minor) = TerraformCommand.get_terraform_version(stdout)
                assert actual_major == major
                assert actual_minor == minor
                mocked.assert_called_once()
