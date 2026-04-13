import os
from unittest.mock import MagicMock, patch

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


class TestSlackStatusBoardBlocks:
    def _make_board(self, title=None, run_id=None):
        from tfworker.handlers.slack import SlackStatusBoard
        b = SlackStatusBoard(channel="#ops", title=title, run_id=run_id)
        b._deployment = "prod"
        return b

    def test_blocks_is_list(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "done")
        blocks = board._build_blocks()
        assert isinstance(blocks, list)
        assert len(blocks) >= 1

    def test_header_block_contains_deployment(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "done")
        blocks = board._build_blocks()
        header = blocks[0]
        assert header["type"] == "header"
        assert "prod" in header["text"]["text"]

    def test_header_uses_title_when_set(self):
        board = self._make_board(title="My Run")
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "done")
        blocks = board._build_blocks()
        assert "My Run" in blocks[0]["text"]["text"]

    def test_header_includes_run_id(self):
        board = self._make_board(run_id="run-42")
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "done")
        blocks = board._build_blocks()
        assert "run-42" in blocks[0]["text"]["text"]

    def test_git_context_block_present_when_available(self):
        board = self._make_board()
        board._git_context = "Branch: main  Commit: abc1234"
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "done")
        blocks = board._build_blocks()
        context_texts = [
            str(b) for b in blocks if b.get("type") == "context"
        ]
        assert any("main" in t for t in context_texts)

    def test_status_table_contains_definition_name(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "done")
        blocks = board._build_blocks()
        all_text = str(blocks)
        assert "vpc" in all_text

    def test_status_table_contains_action_column(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "done")
        blocks = board._build_blocks()
        all_text = str(blocks)
        assert "Plan" in all_text or "plan" in all_text

    def test_banner_in_progress(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "running")
        blocks = board._build_blocks()
        all_text = str(blocks)
        assert "🟡" in all_text

    def test_banner_done(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "done")
        blocks = board._build_blocks()
        all_text = str(blocks)
        assert "✅" in all_text

    def test_banner_failed(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "failed")
        blocks = board._build_blocks()
        all_text = str(blocks)
        assert "❌" in all_text

    def test_only_observed_actions_as_columns(self):
        """A plan-only run shows Init and Plan columns only."""
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.INIT, "done")
        board.mark("vpc", TerraformAction.PLAN, "done")
        blocks = board._build_blocks()
        all_text = str(blocks)
        assert "Apply" not in all_text
        assert "Destroy" not in all_text


class TestSlackStatusBoardPostOrUpdate:
    def _make_board(self):
        from tfworker.handlers.slack import SlackStatusBoard
        b = SlackStatusBoard(channel="#ops", title=None, run_id=None)
        b._deployment = "prod"
        b.ensure_definition("vpc", "prod", "/tmp")
        b.mark("vpc", TerraformAction.PLAN, "running")
        return b

    def _mock_client(self):
        client = MagicMock()
        client.chat_postMessage.return_value = {"ts": "111.222", "channel": "C123"}
        return client

    def test_first_call_uses_post_message(self):
        board = self._make_board()
        client = self._mock_client()
        board.post_or_update(client)
        client.chat_postMessage.assert_called_once()
        client.chat_update.assert_not_called()

    def test_first_call_stores_ts(self):
        board = self._make_board()
        client = self._mock_client()
        board.post_or_update(client)
        assert board._ts == "111.222"

    def test_second_call_uses_update(self):
        board = self._make_board()
        client = self._mock_client()
        board.post_or_update(client)
        board.mark("vpc", TerraformAction.PLAN, "done")
        board.post_or_update(client)
        client.chat_update.assert_called_once()

    def test_update_uses_stored_ts(self):
        board = self._make_board()
        client = self._mock_client()
        board.post_or_update(client)
        board.post_or_update(client)
        call_kwargs = client.chat_update.call_args.kwargs
        assert call_kwargs["ts"] == "111.222"

    def test_slack_error_is_logged_not_raised(self):
        board = self._make_board()
        client = MagicMock()
        client.chat_postMessage.side_effect = Exception("api down")
        # Must not raise
        board.post_or_update(client)

    def test_post_thread_reply_posts_under_parent_ts(self):
        board = self._make_board()
        client = self._mock_client()
        board.post_or_update(client)  # sets _ts
        board.post_thread_reply(client, "all done")
        call_kwargs = client.chat_postMessage.call_args_list[-1].kwargs
        assert call_kwargs["thread_ts"] == "111.222"
        assert call_kwargs["text"] == "all done"

    def test_post_thread_reply_noop_when_no_ts(self):
        board = self._make_board()
        client = self._mock_client()
        # _ts is None — no message posted yet
        board.post_thread_reply(client, "reply")
        client.chat_postMessage.assert_not_called()

    def test_thread_reply_error_is_logged_not_raised(self):
        board = self._make_board()
        client = self._mock_client()
        board.post_or_update(client)
        client.chat_postMessage.side_effect = Exception("network error")
        # Must not raise
        board.post_thread_reply(client, "reply")
