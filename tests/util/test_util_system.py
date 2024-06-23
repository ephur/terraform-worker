from unittest import mock

import pytest

from tfworker.util.system import get_platform, pipe_exec, strip_ansi


def mock_pipe_exec(args, stdin=None, cwd=None, env=None):
    return (0, "".encode(), "".encode())


def mock_tf_version(args: str):
    return (0, args.encode(), "".encode())


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

    def test_strip_ansi(self):
        assert strip_ansi("\x1B[31mHello\x1B[0m") == "Hello"
        assert strip_ansi("\x1B[32mWorld\x1B[0m") == "World"
        assert strip_ansi("\x1B[33mFoo\x1B[0m") == "Foo"
        assert strip_ansi("\x1B[34mBar\x1B[0m") == "Bar"

    @pytest.mark.parametrize(
        "opsys, machine, mock_platform_opsys, mock_platform_machine",
        [
            ("linux", "i386", ["linux2"], ["i386"]),
            ("linux", "arm", ["Linux"], ["arm"]),
            ("linux", "amd64", ["linux"], ["x86_64"]),
            ("linux", "amd64", ["linux"], ["amd64"]),
            ("darwin", "amd64", ["darwin"], ["x86_64"]),
            ("darwin", "amd64", ["darwin"], ["amd64"]),
            ("darwin", "arm", ["darwin"], ["arm"]),
            ("darwin", "arm64", ["darwin"], ["aarch64"]),
        ],
    )
    def test_get_platform(
        self, opsys, machine, mock_platform_opsys, mock_platform_machine
    ):
        with mock.patch("platform.system", side_effect=mock_platform_opsys) as mock1:
            with mock.patch(
                "platform.machine", side_effect=mock_platform_machine
            ) as mock2:
                actual_opsys, actual_machine = get_platform()
                assert opsys == actual_opsys
                assert machine == actual_machine
                mock1.assert_called_once()
                mock2.assert_called_once()
