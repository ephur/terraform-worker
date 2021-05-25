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

from contextlib import contextmanager
from unittest import mock

import pytest
from tfworker.util.system import pipe_exec
from tfworker.util.system import which


# context manager to allow testing exceptions in parameterized tests
@contextmanager
def does_not_raise():
    yield


def mock_pipe_exec(args, stdin=None, cwd=None, env=None):
    return (0, "".encode(), "".encode())


def mock_tf_version(args: str):
    return (0, args.encode(), "".encode())


class TestUtilSystem:
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
            (["/bin/cat", "/usr/bin/env grep foo"], 0, None, "foo", "foo", ""),
        ],
    )
    def test_pipe_exec(self, commands, exit_code, cwd, stdin, stdout, stderr):
        (return_exit_code, return_stdout, return_stderr) = pipe_exec(
            commands, cwd=cwd, stdin=stdin
        )

        assert return_exit_code == exit_code
        assert stdout.encode() in return_stdout.rstrip()
        assert return_stderr.rstrip() in stderr.encode()

    def test_which(self):
        with mock.patch(
            "os.path.isfile",
            side_effect=lambda x: True,
        ):
            with mock.patch("os.access", side_effect=lambda x, y: True):
                assert which("terraform") is not None
