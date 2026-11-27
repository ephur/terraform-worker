"""Tests for the trivy handler's scan execution.

Narrow by design: they cover the subprocess call the handler makes, which the
rest of the suite never exercised.
"""

from pathlib import Path

import pytest

import tfworker.util.log as log
from tfworker.exceptions import HandlerError
from tfworker.handlers.trivy import TrivyConfig, TrivyHandler


@pytest.fixture
def handler(tmp_path):
    # the handler refuses to construct unless the binary is runnable, and the
    # scan itself is mocked, so a no-op executable is enough
    trivy = tmp_path / "trivy"
    trivy.write_text("#!/bin/sh\nexit 0\n")
    trivy.chmod(0o755)
    return TrivyHandler(TrivyConfig(path=str(trivy)))


class TestTrivyScan:
    def test_scan_delegates_to_pipe_exec_logged(self, handler, mocker, tmp_path):
        run = mocker.patch(
            "tfworker.handlers.trivy.pipe_exec_logged", return_value=(0, b"", b"")
        )

        handler._scan(tmp_path)

        assert run.call_count == 1
        kwargs = run.call_args.kwargs
        assert kwargs["label"] == "trivy"
        assert kwargs["cwd"] == str(tmp_path)
        assert kwargs["extra"] == {
            "definition_path": str(tmp_path),
            "handler": "trivy",
        }
        assert kwargs["stream_output"] is True
        # the scan itself, not the plan variant
        assert "fs" in run.call_args.args[0]

    def test_scan_of_planfile_uses_config_subcommand(self, handler, mocker, tmp_path):
        run = mocker.patch(
            "tfworker.handlers.trivy.pipe_exec_logged", return_value=(0, b"", b"")
        )
        planfile = tmp_path / "plan.tfplan"
        planfile.touch()

        handler._scan(tmp_path, planfile=planfile)

        assert "config" in run.call_args.args[0]
        assert str(Path.resolve(planfile)) in run.call_args.args[0]

    def test_scan_failure_raises_handler_error(self, handler, mocker, tmp_path):
        mocker.patch(
            "tfworker.handlers.trivy.pipe_exec_logged",
            side_effect=OSError("trivy missing"),
        )

        with pytest.raises(HandlerError, match="Error executing trivy scan"):
            handler._scan(tmp_path)

    def test_scan_json_mode_aggregates_result(self, handler, mocker, tmp_path):
        """In JSON mode the scan output arrives as one structured record."""
        log.log_format = log.LogFormat.JSON
        mocker.patch("tfworker.util.system.pipe_exec", return_value=(0, b"clean", b""))
        aggregate = mocker.patch("tfworker.util.system.log.log_subprocess_result")

        handler._scan(tmp_path)

        assert aggregate.call_args.kwargs["command"] == "trivy"
        assert aggregate.call_args.kwargs["extra"]["handler"] == "trivy"
