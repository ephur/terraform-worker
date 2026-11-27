from unittest import mock

import pytest

import tfworker.util.log as log
from tfworker.util.system import get_platform, pipe_exec, pipe_exec_logged, strip_ansi


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
        return_exit_code, return_stdout, return_stderr = pipe_exec(
            commands, cwd=cwd, stdin=stdin, stream_output=stream_output
        )

        assert return_exit_code == exit_code
        assert stdout.encode() in return_stdout.rstrip()
        assert return_stderr.rstrip() in stderr.encode()

    def test_strip_ansi(self):
        assert strip_ansi("\x1b[31mHello\x1b[0m") == "Hello"
        assert strip_ansi("\x1b[32mWorld\x1b[0m") == "World"
        assert strip_ansi("\x1b[33mFoo\x1b[0m") == "Foo"
        assert strip_ansi("\x1b[34mBar\x1b[0m") == "Bar"

    def test_pipe_exec_streams_through_logger(self, mocker):
        log_call = mocker.patch("tfworker.util.system.log.log")
        printer = mocker.patch("builtins.print")

        exit_code, stdout, stderr = pipe_exec(
            "/bin/echo foo",
            stream_output=True,
            stream_log_level=log.LogLevel.INFO,
        )

        assert exit_code == 0
        assert stdout.rstrip() == b"foo"
        assert stderr == b""
        log_call.assert_called_once_with("foo", level=log.LogLevel.INFO)
        printer.assert_not_called()

    def test_pipe_exec_json_mode_does_not_emit_live_lines(self, mocker):
        old_format = log.log_format
        log.log_format = log.LogFormat.JSON
        log_call = mocker.patch("tfworker.util.system.log.log")
        printer = mocker.patch("builtins.print")

        exit_code, stdout, stderr = pipe_exec(
            "/bin/echo foo",
            stream_output=True,
            stream_log_level=log.LogLevel.INFO,
        )

        assert exit_code == 0
        assert stdout.rstrip() == b"foo"
        assert stderr == b""
        log_call.assert_not_called()
        printer.assert_not_called()
        log.log_format = old_format

    # ------------------------------------------------------------------
    # pipe_exec_logged: one place deciding stream vs aggregate
    # ------------------------------------------------------------------
    def test_pipe_exec_logged_text_streams_and_does_not_aggregate(self, mocker):
        exec_fn = mocker.patch(
            "tfworker.util.system.pipe_exec", return_value=(0, b"out", b"")
        )
        aggregate = mocker.patch("tfworker.util.system.log.log_subprocess_result")
        log.log_format = log.LogFormat.TEXT

        result = pipe_exec_logged(
            "terraform init", label="terraform init", cwd="/tmp", env={"A": "B"}
        )

        assert result == (0, b"out", b"")
        assert exec_fn.call_args.kwargs["stream_output"] is True
        assert exec_fn.call_args.kwargs["stream_log_level"] == log.LogLevel.INFO
        assert exec_fn.call_args.kwargs["env"] == {"A": "B"}
        aggregate.assert_not_called()

    def test_pipe_exec_logged_json_aggregates_and_does_not_stream(self, mocker):
        exec_fn = mocker.patch(
            "tfworker.util.system.pipe_exec", return_value=(0, b"out", b"err")
        )
        aggregate = mocker.patch("tfworker.util.system.log.log_subprocess_result")
        log.log_format = log.LogFormat.JSON

        pipe_exec_logged(
            "terraform init",
            label="terraform init",
            extra={"definition": "example"},
            message="terraform init output for example",
        )

        assert exec_fn.call_args.kwargs["stream_output"] is False
        assert "stream_log_level" not in exec_fn.call_args.kwargs
        aggregate.assert_called_once_with(
            command="terraform init",
            exit_code=0,
            stdout=b"out",
            stderr=b"err",
            level=log.LogLevel.INFO,
            extra={"definition": "example"},
            message="terraform init output for example",
        )

    def test_pipe_exec_logged_failure_logs_at_error(self, mocker):
        mocker.patch("tfworker.util.system.pipe_exec", return_value=(1, b"", b"boom"))
        aggregate = mocker.patch("tfworker.util.system.log.log_subprocess_result")
        log.log_format = log.LogFormat.JSON

        pipe_exec_logged("terraform init", label="terraform init")

        assert aggregate.call_args.kwargs["level"] == log.LogLevel.ERROR

    def test_pipe_exec_logged_ok_exit_codes(self, mocker):
        """terraform plan returns 2 for changes, which is not a failure."""
        mocker.patch("tfworker.util.system.pipe_exec", return_value=(2, b"", b""))
        aggregate = mocker.patch("tfworker.util.system.log.log_subprocess_result")
        log.log_format = log.LogFormat.JSON

        pipe_exec_logged("terraform plan", label="terraform plan", ok_exit_codes=(0, 2))
        assert aggregate.call_args.kwargs["level"] == log.LogLevel.INFO

        pipe_exec_logged("terraform apply", label="terraform apply")
        assert aggregate.call_args.kwargs["level"] == log.LogLevel.ERROR

    def test_pipe_exec_logged_success_level_override(self, mocker):
        mocker.patch("tfworker.util.system.pipe_exec", return_value=(0, b"", b""))
        aggregate = mocker.patch("tfworker.util.system.log.log_subprocess_result")
        log.log_format = log.LogFormat.JSON

        pipe_exec_logged("hook", label="hook", success_level=log.LogLevel.DEBUG)

        assert aggregate.call_args.kwargs["level"] == log.LogLevel.DEBUG

    def test_pipe_exec_logged_debug_when_not_streaming(self, mocker):
        mocker.patch("tfworker.util.system.pipe_exec", return_value=(0, b"out", b"err"))
        debug = mocker.patch("tfworker.util.system.log.debug")
        log.log_format = log.LogFormat.TEXT

        pipe_exec_logged(
            "terraform get",
            label="terraform get",
            stream_output=False,
            debug_when_not_streaming=True,
        )

        debug.assert_any_call("terraform get result: out")
        debug.assert_any_call("terraform get error: err")

    def test_pipe_exec_logged_omits_env_when_not_given(self, mocker):
        exec_fn = mocker.patch(
            "tfworker.util.system.pipe_exec", return_value=(0, b"", b"")
        )
        log.log_format = log.LogFormat.TEXT

        pipe_exec_logged("terraform get", label="terraform get")

        assert "env" not in exec_fn.call_args.kwargs

    def test_pipe_exec_logged_uses_supplied_exec_fn(self, mocker):
        default_exec = mocker.patch("tfworker.util.system.pipe_exec")
        other = mocker.Mock(return_value=(0, b"", b""))
        log.log_format = log.LogFormat.TEXT

        pipe_exec_logged("hook.sh", label="hook", exec_fn=other)

        other.assert_called_once()
        default_exec.assert_not_called()

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
