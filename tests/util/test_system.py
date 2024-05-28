# Copyright 2020-2023 Richard Maynard (richard.maynard@gmail.com)
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

from tfworker.util.system import get_version, pipe_exec, strip_ansi, which


# context manager to allow testing exceptions in parameterized tests
@contextmanager
def does_not_raise():
    yield


def mock_pipe_exec(args, stdin=None, cwd=None, env=None):
    return (0, "".encode(), "".encode())


def mock_tf_version(args: str):
    return (0, args.encode(), "".encode())


def mock_distribution(*args, **kwargs):
    Class = mock.MagicMock()
    Class.version = "1.2.3"
    return Class


class TestUtilSystem:
    @pytest.mark.parametrize(
        "commands, exit_code, cwd, stdin, stdout, stderr, stream_output",
        [
            ("/usr/bin/env true", 0, None, None, "", "", False),
            ("/usr/bin/env true", 0, None, None, "", "", True),
            ("/usr/bin/env false", 1, None, None, "", "", False),
            ("/usr/bin/env false", 1, None, None, "", "", True),
            ("/bin/echo foo", 0, None, None, "foo", "", False),
            ("/bin/echo foo", 0, None, None, "foo", "", True),
            ("/usr/bin/env grep foo", 0, None, "foo", "foo", "", False),
            ("/usr/bin/env grep foo", 0, None, "foo", "foo", "", True),
            ("/bin/pwd", 0, "/tmp", None, "/tmp", "", False),
            ("/bin/pwd", 0, "/tmp", None, "/tmp", "", True),
            (
                "/bin/cat /yisohwo0AhK8Ah ",
                1,
                None,
                None,
                "",
                "/bin/cat: /yisohwo0AhK8Ah: No such file or directory",
                False,
            ),
            (
                "/bin/cat /yisohwo0AhK8Ah ",
                1,
                None,
                None,
                "",
                "/bin/cat: /yisohwo0AhK8Ah: No such file or directory",
                True,
            ),
            (
                ["/bin/echo foo", "/usr/bin/env grep foo"],
                0,
                None,
                None,
                "foo",
                "",
                False,
            ),
            (
                ["/bin/echo foo", "/usr/bin/env grep foo"],
                0,
                None,
                None,
                "foo",
                "",
                True,
            ),
            (["/bin/echo foo", "/usr/bin/env grep bar"], 1, None, None, "", "", False),
            (["/bin/echo foo", "/usr/bin/env grep bar"], 1, None, None, "", "", True),
            (["/bin/cat", "/usr/bin/env grep foo"], 0, None, "foo", "foo", "", False),
            (["/bin/cat", "/usr/bin/env grep foo"], 0, None, "foo", "foo", "", True),
        ],
    )
    @pytest.mark.timeout(2)
    def test_pipe_exec(
        self, commands, exit_code, cwd, stdin, stdout, stderr, stream_output
    ):
        (return_exit_code, return_stdout, return_stderr) = pipe_exec(
            commands, cwd=cwd, stdin=stdin, stream_output=stream_output
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

    def test_which_full_path(self):
        with mock.patch(
            "os.path.isfile",
            side_effect=lambda x: True,
        ):
            with mock.patch("os.access", side_effect=lambda x, y: True):
                assert which("/full/path/to/file") is not None

    def test_which_not_found(self):
        with mock.patch(
            "os.path.isfile",
            side_effect=lambda x: False,
        ):
            with mock.patch("os.access", side_effect=lambda x, y: False):
                assert which("terraform") is None

    def test_get_version(self):
        with mock.patch(
            "tfworker.util.system.get_distribution",
            side_effect=mock_distribution,
        ):
            assert get_version() == "1.2.3"

    def test_get_version_unknown(self):
        from pkg_resources import DistributionNotFound

        with mock.patch(
            "tfworker.util.system.get_distribution",
            side_effect=DistributionNotFound,
        ):
            assert get_version() == "unknown"

    def test_strip_ansi(self):
        assert strip_ansi("\x1B[31mHello\x1B[0m") == "Hello"
        assert strip_ansi("\x1B[32mWorld\x1B[0m") == "World"
        assert strip_ansi("\x1B[33mFoo\x1B[0m") == "Foo"
        assert strip_ansi("\x1B[34mBar\x1B[0m") == "Bar"
