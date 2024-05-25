import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from tfworker.handlers.exceptions import HandlerError
from tfworker.handlers.trivy import TrivyHandler


class TestTrivyHandlerTrivyRunnable(unittest.TestCase):
    def test_trivy_not_runnable(self):
        with self.assertRaises(HandlerError):
            TrivyHandler({"path": "/path/to/trivy"})

    @patch("os.path.exists")
    @patch("os.access")
    def test_trivy_runnable(self, mock_access, mock_exists):
        mock_exists.return_value = True
        mock_access.return_value = True
        self.assertTrue(TrivyHandler._trivy_runable("/path/to/trivy"))

    @patch("os.path.exists")
    def test_trivy_not_runnable_no_exists(self, mock_exists):
        mock_exists.return_value = False
        self.assertFalse(TrivyHandler._trivy_runable("/path/to/trivy"))

    @patch("os.path.exists")
    @patch("os.access")
    def test_trivy_not_runnable_no_access(self, mock_access, mock_exists):
        mock_exists.return_value = True
        mock_access.return_value = False
        self.assertFalse(TrivyHandler._trivy_runable("/path/to/trivy"))


class TestTrivyHandlerExecute(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def trivy_runnable_patcher(self):
        patcher = patch("tfworker.handlers.trivy.TrivyHandler._trivy_runable")
        mock_trivy_runable = patcher.start()
        mock_trivy_runable.return_value = True
        yield mock_trivy_runable

    def test__raise_if_not_ready(self):
        handler = TrivyHandler({})
        handler._ready = False
        with self.assertRaises(HandlerError):
            handler.execute("plan", "pre")

    def test_execute_pre_plan_without_definition_path(self):
        handler = TrivyHandler({})
        with self.assertRaises(HandlerError):
            handler.execute("plan", "pre")

    @patch("tfworker.handlers.trivy.click")
    def test_execute_pre_plan_skip_definition(self, mock_click):
        handler = TrivyHandler({"skip_definition": True})
        handler._trivy_runable = MagicMock(return_value=True)
        handler.is_ready = MagicMock(return_value=True)
        handler.execute("plan", "pre", definition_path="/path/to/definition")
        mock_click.secho.assert_called_with(
            "Skipping trivy scan of definition", fg="yellow"
        )

    @patch("tfworker.handlers.trivy.click")
    def test_execute_pre_plan_scan_definition(self, mock_click):
        handler = TrivyHandler({})
        handler.is_ready = MagicMock(return_value=True)
        handler._scan = MagicMock()
        handler.execute("plan", "pre", definition_path="/path/to/definition")
        mock_click.secho.assert_called_with(
            "scanning definition with trivy: /path/to/definition", fg="green"
        )
        handler._scan.assert_called_with("/path/to/definition")

    def test_execute_post_plan_without_planfile(self):
        handler = TrivyHandler({})
        with self.assertRaises(HandlerError):
            handler.execute("plan", "post", changes=True)

    def test_execute_post_plan_without_definition_path(self):
        handler = TrivyHandler({})
        with self.assertRaises(HandlerError):
            handler.execute("plan", "post", planfile="/path/to/planfile", changes=True)

    @patch("tfworker.handlers.trivy.click")
    def test_execute_post_plan_skip_planfile(self, mock_click):
        handler = TrivyHandler({"skip_planfile": True})
        handler.is_ready = MagicMock(return_value=True)
        handler.execute(
            "plan",
            "post",
            planfile="/path/to/planfile",
            definition_path="/path/to/definition",
            changes=True,
        )
        mock_click.secho.assert_called_with(
            "Skipping trivy scan of planfile", fg="yellow"
        )

    @patch("tfworker.handlers.trivy.click")
    def test_execute_post_plan_scan_planfile(self, mock_click):
        handler = TrivyHandler({})
        handler.is_ready = MagicMock(return_value=True)
        handler._scan = MagicMock()
        handler.execute(
            "plan",
            "post",
            planfile="/path/to/planfile",
            definition_path="/path/to/definition",
            changes=True,
        )
        mock_click.secho.assert_called_with(
            "scanning planfile with trivy: /path/to/planfile", fg="green"
        )
        handler._scan.assert_called_with("/path/to/definition", "/path/to/planfile")


class TestTrivyHandlerScan(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def trivy_runnable_patcher(self):
        patcher = patch("tfworker.handlers.trivy.TrivyHandler._trivy_runable")
        mock_trivy_runable = patcher.start()
        mock_trivy_runable.return_value = True
        yield mock_trivy_runable

    @patch("tfworker.handlers.trivy.pipe_exec")
    @patch("tfworker.handlers.trivy.click")
    def test__scan_definition_success_with_defaults(self, mock_click, mock_pipe_exec):
        mock_pipe_exec.return_value = (0, "stdout", "stderr")
        handler = TrivyHandler({})
        handler._trivy_runable = MagicMock(return_value=True)
        handler._handle_results = MagicMock()
        handler._scan("/path/to/definition")
        mock_pipe_exec.assert_called_with(
            "/usr/bin/trivy --quiet fs --scanners misconfig,secret --skip-dirs **/examples --cache-dir /tmp/trivy_cache --severity HIGH,CRITICAL --exit-code 1 .",
            stream_output=True,
            cwd="/path/to/definition",
        )
        handler._handle_results.assert_called_with(0, "stdout", "stderr", None)

    @patch("tfworker.handlers.trivy.pipe_exec")
    @patch("tfworker.handlers.trivy.click")
    def test__scan_plan_success_with_options(self, mock_click, mock_pipe_exec):
        config = {
            "path": "/path/to/trivy",
            "exit_code": "2",
            "skip_dirs": [],
            "severity": "CRITICAL",
            "cache_dir": "/path/to/cache",
            "stream_output": True,
            "quiet": False,
            "debug": True,
            "stream_output": False,
            "format": "template",
            "template": "template",
            "args": {"arg1": "value1", "arg2": "value2"},
        }

        mock_pipe_exec.return_value = (0, "stdout", "stderr")
        handler = TrivyHandler(config)
        handler._trivy_runable = MagicMock(return_value=True)
        handler._handle_results = MagicMock()
        handler._scan("/path/to/definition")
        mock_pipe_exec.assert_called_with(
            "/path/to/trivy --debug fs --scanners misconfig,secret --cache-dir /path/to/cache --severity CRITICAL --exit-code 2 --format template --template template --arg1 value1 --arg2 value2 .",
            stream_output=False,
            cwd="/path/to/definition",
        )
        handler._handle_results.assert_called_with(0, "stdout", "stderr", None)

    @patch("tfworker.handlers.trivy.pipe_exec")
    @patch("tfworker.handlers.trivy.click")
    def test__scan_planfile_success_with_defaults(self, mock_click, mock_pipe_exec):
        mock_pipe_exec.return_value = (0, "stdout", "stderr")
        handler = TrivyHandler({})
        handler._trivy_runable = MagicMock(return_value=True)
        handler._handle_results = MagicMock()
        handler._scan("/path/to/definition", Path("/path/to/planfile"))
        mock_pipe_exec.assert_called_with(
            "/usr/bin/trivy --quiet config --cache-dir /tmp/trivy_cache --severity HIGH,CRITICAL --exit-code 1 /path/to/planfile",
            stream_output=True,
            cwd="/path/to/definition",
        )
        handler._handle_results.assert_called_with(
            0, "stdout", "stderr", Path("/path/to/planfile")
        )

    @patch("tfworker.handlers.trivy.pipe_exec")
    @patch("tfworker.handlers.trivy.click")
    def test__scan_planfile_success_with_options(self, mock_click, mock_pipe_exec):
        config = {
            "path": "/path/to/trivy",
            "exit_code": "2",
            "skip_dirs": [],
            "severity": "CRITICAL",
            "cache_dir": "/path/to/cache",
            "stream_output": True,
            "quiet": False,
            "debug": True,
            "stream_output": False,
            "format": "template",
            "template": "template",
            "args": {"arg1": "value1", "arg2": "value2"},
        }

        mock_pipe_exec.return_value = (0, "stdout", "stderr")
        handler = TrivyHandler(config)
        handler._trivy_runable = MagicMock(return_value=True)
        handler._handle_results = MagicMock()
        handler._scan("/path/to/definition", Path("/path/to/planfile"))
        mock_pipe_exec.assert_called_with(
            "/path/to/trivy --debug config --cache-dir /path/to/cache --severity CRITICAL --exit-code 2 --format template --template template --arg1 value1 --arg2 value2 /path/to/planfile",
            stream_output=False,
            cwd="/path/to/definition",
        )
        handler._handle_results.assert_called_with(
            0, "stdout", "stderr", Path("/path/to/planfile")
        )

    @patch("tfworker.handlers.trivy.pipe_exec")
    @patch("tfworker.handlers.trivy.click")
    def test__scan_failure(self, mock_click, mock_pipe_exec):
        mock_pipe_exec.side_effect = Exception("error")
        handler = TrivyHandler({})
        handler._trivy_runable = MagicMock(return_value=True)
        handler._handle_results = MagicMock()
        with self.assertRaises(HandlerError):
            handler._scan("/path/to/definition")
        handler._handle_results.assert_not_called()


class TestTrivyHandlerHandleResults(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def trivy_runnable_patcher(self):
        patcher = patch("tfworker.handlers.trivy.TrivyHandler._trivy_runable")
        mock_trivy_runable = patcher.start()
        mock_trivy_runable.return_value = True
        yield mock_trivy_runable

    @patch("tfworker.handlers.trivy.click")
    def test__handle_results_success(self, mock_click):
        handler = TrivyHandler({})
        handler._handle_results(0, "stdout".encode(), "stderr".encode(), None)
        mock_click.secho.assert_not_called()

    @patch("tfworker.handlers.trivy.click")
    def test__handle_results_failure(self, mock_click):
        handler = TrivyHandler({})
        handler._handle_results(1, "stdout".encode(), "stderr".encode(), None)
        mock_click.secho.assert_called_with(
            "trivy scan failed with exit code 1", fg="red"
        )
        mock_click.secho.assert_called_once()

    @patch("tfworker.handlers.trivy.click")
    @patch("tfworker.handlers.trivy.strip_ansi")
    def test__handle_results_failure_stream_output(self, mock_strip_ansi, mock_click):
        handler = TrivyHandler({"stream_output": False})
        mock_strip_ansi.side_effect = MagicMock(
            side_effect=lambda x: x.decode("UTF-8") if isinstance(x, bytes) else x
        )
        handler._handle_results(1, "stdout".encode(), "stderr".encode(), None)
        calls = [
            call("trivy scan failed with exit code 1", fg="red"),
            call("stdout: stdout", fg="red"),
            call("stderr: stderr", fg="red"),
        ]
        mock_click.secho.assert_has_calls(calls)

    @patch("tfworker.handlers.trivy.click")
    def test__handle_results_required(self, mock_click):
        handler = TrivyHandler({"required": True})
        with self.assertRaises(HandlerError):
            handler._handle_results(1, "stdout".encode(), "stderr".encode(), None)

    @patch("tfworker.handlers.trivy.click")
    @patch("os.remove")
    def test__handle_results_remove_planfile(self, mock_remove, mock_click):
        handler = TrivyHandler({"required": True})
        with self.assertRaises(HandlerError):
            handler._handle_results(
                1, "stdout".encode(), "stderr".encode(), "/path/to/planfile"
            )
        mock_remove.assert_called_with("/path/to/planfile")


class TestTrivyHandlerRaiseIfNotReady(unittest.TestCase):
    def test__raise_if_not_ready_ready(self):
        handler = TrivyHandler({})
        handler._ready = True
        result = handler._raise_if_not_ready()
        self.assertIsNone(result)

    def test__raise_if_not_ready_not_ready(self):
        handler = TrivyHandler({"required": False})
        handler._ready = False
        with self.assertRaises(HandlerError) as e:
            handler._raise_if_not_ready()
            self.assertFalse(e.terminate)

    def test__raise_if_not_ready_required(self):
        handler = TrivyHandler({"required": True})
        handler._ready = False
        with self.assertRaises(HandlerError) as e:
            handler._raise_if_not_ready()
            self.assertTrue(e.terminate)


if __name__ == "__main__":
    unittest.main()
