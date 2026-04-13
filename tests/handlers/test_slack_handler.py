import os
from unittest.mock import patch

import pytest

from tfworker.custom_types.terraform import TerraformAction, TerraformStage


class TestSlackConfig:
    def test_requires_channel(self):
        """channel is required."""
        from tfworker.handlers.slack import SlackConfig
        with pytest.raises(Exception):
            SlackConfig()

    def test_token_raw_value(self):
        """token field is used directly when provided."""
        from tfworker.handlers.slack import SlackConfig
        cfg = SlackConfig(channel="#ops", token="xoxb-raw")
        assert cfg.resolved_token == "xoxb-raw"

    def test_token_from_default_env_var(self):
        """Falls back to SLACK_BOT_TOKEN env var."""
        from tfworker.handlers.slack import SlackConfig
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-from-env"}):
            cfg = SlackConfig(channel="#ops")
            assert cfg.resolved_token == "xoxb-from-env"

    def test_token_from_custom_env_var(self):
        """token_env overrides the default env var name."""
        from tfworker.handlers.slack import SlackConfig
        with patch.dict(os.environ, {"MY_TOKEN": "xoxb-custom"}, clear=False):
            cfg = SlackConfig(channel="#ops", token_env="MY_TOKEN")
            assert cfg.resolved_token == "xoxb-custom"

    def test_token_raw_takes_precedence_over_env(self):
        """Raw token takes precedence over env var."""
        from tfworker.handlers.slack import SlackConfig
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-env"}):
            cfg = SlackConfig(channel="#ops", token="xoxb-raw")
            assert cfg.resolved_token == "xoxb-raw"

    def test_missing_token_raises(self):
        """Raises ValueError when no token is resolvable."""
        from tfworker.handlers.slack import SlackConfig
        env = {k: v for k, v in os.environ.items() if k != "SLACK_BOT_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises((ValueError, Exception)):
                SlackConfig(channel="#ops")

    def test_defaults(self):
        """Default values are correct."""
        from tfworker.handlers.slack import SlackConfig
        cfg = SlackConfig(channel="#ops", token="xoxb-x")
        assert cfg.token_env == "SLACK_BOT_TOKEN"
        assert cfg.thread_reply is False
        assert cfg.title is None
        assert cfg.thread_reply_text is None


class TestSlackStatusBoard:
    def _make_board(self):
        from tfworker.handlers.slack import SlackStatusBoard
        return SlackStatusBoard(channel="#ops", title=None, run_id=None)

    def test_ensure_definition_registers_definition(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        assert "vpc" in board._statuses

    def test_ensure_definition_captures_deployment_once(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.ensure_definition("eks", "other", "/tmp")
        assert board._deployment == "prod"  # first call wins

    def test_mark_sets_status(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "running")
        assert board._statuses["vpc"]["plan"] == "running"

    def test_mark_adds_action_to_seen_actions(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "running")
        assert "plan" in board._seen_actions

    def test_mark_maintains_action_order(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.APPLY, "running")
        board.mark("vpc", TerraformAction.INIT, "done")
        board.mark("vpc", TerraformAction.PLAN, "done")
        assert board._seen_actions == ["init", "plan", "apply"]

    def test_is_terminal_false_when_running(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "running")
        assert board.is_terminal() is False

    def test_is_terminal_false_when_pending(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.INIT, "done")
        board.mark("vpc", TerraformAction.PLAN, "running")
        assert board.is_terminal() is False

    def test_is_terminal_true_when_all_done(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.INIT, "done")
        board.mark("vpc", TerraformAction.PLAN, "done")
        assert board.is_terminal() is True

    def test_is_terminal_true_when_failed(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "failed")
        assert board.is_terminal() is True

    def test_overall_status_in_progress(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "running")
        assert board.overall_status() == "in_progress"

    def test_overall_status_done(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "done")
        assert board.overall_status() == "done"

    def test_overall_status_failed(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "failed")
        assert board.overall_status() == "failed"

    def test_failed_count(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.ensure_definition("eks", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "failed")
        board.mark("eks", TerraformAction.PLAN, "done")
        assert board.failed_count() == 1


class TestSlackStatusBoardGitContext:
    def _make_board(self):
        from tfworker.handlers.slack import SlackStatusBoard
        return SlackStatusBoard(channel="#ops", title=None, run_id=None)

    def test_github_actions_env_vars(self):
        board = self._make_board()
        env = {
            "GITHUB_REF_NAME": "main",
            "GITHUB_SHA": "abc1234567",
            "CI_COMMIT_REF_NAME": "",
            "CI_COMMIT_SHA": "",
        }
        with patch.dict(os.environ, env, clear=True):
            result = board._resolve_git_context("/tmp")
        assert "main" in result
        assert "abc1234" in result

    def test_gitlab_ci_env_vars(self):
        board = self._make_board()
        env = {
            "CI_COMMIT_REF_NAME": "feature/x",
            "CI_COMMIT_SHA": "def7890123",
            "GITHUB_REF_NAME": "",
            "GITHUB_SHA": "",
        }
        with patch.dict(os.environ, env, clear=True):
            result = board._resolve_git_context("/tmp")
        assert "feature/x" in result
        assert "def7890" in result

    def test_falls_back_to_git_subprocess(self):
        board = self._make_board()
        env = {k: v for k, v in os.environ.items()
               if k not in ("GITHUB_REF_NAME", "GITHUB_SHA",
                            "CI_COMMIT_REF_NAME", "CI_COMMIT_SHA")}
        with patch.dict(os.environ, env, clear=True):
            with patch("subprocess.check_output") as mock_sub:
                mock_sub.side_effect = [b"mybranch\n", b"a1b2c3d\n"]
                result = board._resolve_git_context("/some/dir")
        assert "mybranch" in result
        assert "a1b2c3d" in result

    def test_returns_none_on_complete_failure(self):
        board = self._make_board()
        env = {k: v for k, v in os.environ.items()
               if k not in ("GITHUB_REF_NAME", "GITHUB_SHA",
                            "CI_COMMIT_REF_NAME", "CI_COMMIT_SHA")}
        with patch.dict(os.environ, env, clear=True):
            with patch("subprocess.check_output", side_effect=Exception("no git")):
                result = board._resolve_git_context("/some/dir")
        assert result is None
