import os
from unittest.mock import MagicMock, patch

import pytest

from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.commands.terraform import TerraformResult
from tfworker.definitions.model import Definition


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


class TestSlackHandlerInit:
    def _make_config(self, **kwargs):
        from tfworker.handlers.slack import SlackConfig
        return SlackConfig(channel="#ops", token="xoxb-test", **kwargs)

    def test_is_ready_after_init(self):
        from tfworker.handlers.slack import SlackHandler
        with patch("tfworker.handlers.slack.WebClient"):
            handler = SlackHandler(self._make_config())
        assert handler.is_ready() is True

    def test_missing_token_raises_handler_error(self):
        from tfworker.handlers.slack import SlackConfig, SlackHandler
        env = {k: v for k, v in os.environ.items() if k != "SLACK_BOT_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception):
                SlackConfig(channel="#ops")

    def test_handler_stores_board(self):
        from tfworker.handlers.slack import SlackHandler, SlackStatusBoard
        with patch("tfworker.handlers.slack.WebClient"):
            handler = SlackHandler(self._make_config())
        assert isinstance(handler._board, SlackStatusBoard)

    def test_actions_includes_all_terraform_actions(self):
        from tfworker.handlers.slack import SlackHandler
        assert TerraformAction.INIT in SlackHandler.actions
        assert TerraformAction.PLAN in SlackHandler.actions
        assert TerraformAction.APPLY in SlackHandler.actions
        assert TerraformAction.DESTROY in SlackHandler.actions


class TestSlackHandlerExecute:
    def _make_handler(self):
        from tfworker.handlers.slack import SlackConfig, SlackHandler
        cfg = SlackConfig(channel="#ops", token="xoxb-test")
        with patch("tfworker.handlers.slack.WebClient"):
            handler = SlackHandler(cfg)
        handler._client = MagicMock()
        handler._client.chat_postMessage.return_value = {
            "ts": "1.2", "channel": "C1"
        }
        return handler

    def _make_definition(self, name="vpc"):
        return Definition(name=name, path="/tmp")

    def test_pre_marks_running_and_posts(self):
        handler = self._make_handler()
        defn = self._make_definition()
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", defn, "/tmp")
        assert handler._board._statuses["vpc"]["plan"] == "running"
        handler._client.chat_postMessage.assert_called_once()

    def test_post_success_marks_done_and_updates(self):
        handler = self._make_handler()
        defn = self._make_definition()
        result = TerraformResult(0, b"ok", b"")
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", defn, "/tmp")
        handler.execute(TerraformAction.PLAN, TerraformStage.POST, "prod", defn, "/tmp", result)
        assert handler._board._statuses["vpc"]["plan"] == "done"
        handler._client.chat_update.assert_called_once()

    def test_post_failure_marks_failed_and_updates(self):
        handler = self._make_handler()
        defn = self._make_definition()
        result = TerraformResult(1, b"", b"error!")
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", defn, "/tmp")
        handler.execute(TerraformAction.PLAN, TerraformStage.POST, "prod", defn, "/tmp", result)
        assert handler._board._statuses["vpc"]["plan"] == "failed"

    def test_multiple_definitions_tracked(self):
        handler = self._make_handler()
        vpc = self._make_definition("vpc")
        eks = self._make_definition("eks")
        handler.execute(TerraformAction.INIT, TerraformStage.PRE, "prod", vpc, "/tmp")
        handler.execute(TerraformAction.INIT, TerraformStage.PRE, "prod", eks, "/tmp")
        assert "vpc" in handler._board._statuses
        assert "eks" in handler._board._statuses

    def test_execute_never_raises_on_slack_failure(self):
        handler = self._make_handler()
        handler._client.chat_postMessage.side_effect = Exception("Slack is down")
        defn = self._make_definition()
        # Must not raise
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", defn, "/tmp")


class TestSlackHandlerThreadReply:
    def _make_handler(self, thread_reply=True, thread_reply_text=None):
        from tfworker.handlers.slack import SlackConfig, SlackHandler
        cfg = SlackConfig(
            channel="#ops",
            token="xoxb-test",
            thread_reply=thread_reply,
            thread_reply_text=thread_reply_text,
        )
        with patch("tfworker.handlers.slack.WebClient"):
            handler = SlackHandler(cfg)
        handler._client = MagicMock()
        handler._client.chat_postMessage.return_value = {
            "ts": "1.2", "channel": "C1"
        }
        return handler

    def _make_definition(self, name="vpc"):
        return Definition(name=name, path="/tmp")

    def test_error_reply_posted_on_failure(self):
        handler = self._make_handler()
        defn = self._make_definition()
        result = TerraformResult(1, b"", b"something went wrong")
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", defn, "/tmp")
        handler.execute(TerraformAction.PLAN, TerraformStage.POST, "prod", defn, "/tmp", result)
        # chat_postMessage called twice: initial post + error thread reply
        assert handler._client.chat_postMessage.call_count == 2
        thread_call = handler._client.chat_postMessage.call_args_list[-1]
        assert thread_call.kwargs["thread_ts"] == "1.2"
        assert "something went wrong" in thread_call.kwargs["text"]

    def test_completion_reply_posted_on_terminal_success(self):
        handler = self._make_handler()
        defn = self._make_definition()
        result = TerraformResult(0, b"ok", b"")
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", defn, "/tmp")
        handler.execute(TerraformAction.PLAN, TerraformStage.POST, "prod", defn, "/tmp", result)
        assert handler._client.chat_postMessage.call_count == 2
        thread_call = handler._client.chat_postMessage.call_args_list[-1]
        assert thread_call.kwargs["thread_ts"] == "1.2"

    def test_custom_thread_reply_text_rendered(self):
        handler = self._make_handler(
            thread_reply_text="Run {run_id} done: {status} for {deployment}"
        )
        handler._board._run_id = "abc-123"
        defn = self._make_definition()
        result = TerraformResult(0, b"ok", b"")
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", defn, "/tmp")
        handler.execute(TerraformAction.PLAN, TerraformStage.POST, "prod", defn, "/tmp", result)
        thread_call = handler._client.chat_postMessage.call_args_list[-1]
        assert "abc-123" in thread_call.kwargs["text"]
        assert "prod" in thread_call.kwargs["text"]

    def test_no_thread_reply_when_disabled(self):
        handler = self._make_handler(thread_reply=False)
        defn = self._make_definition()
        result = TerraformResult(0, b"ok", b"")
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", defn, "/tmp")
        handler.execute(TerraformAction.PLAN, TerraformStage.POST, "prod", defn, "/tmp", result)
        # Only the initial post — no thread reply
        assert handler._client.chat_postMessage.call_count == 1

    def test_completion_reply_not_posted_mid_run(self):
        """No completion reply while other definitions are still pending."""
        handler = self._make_handler()
        vpc = self._make_definition("vpc")
        eks = self._make_definition("eks")
        result_ok = TerraformResult(0, b"ok", b"")
        # Register eks as in-flight so vpc completing is not terminal
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", eks, "/tmp")
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", vpc, "/tmp")
        handler.execute(TerraformAction.PLAN, TerraformStage.POST, "prod", vpc, "/tmp", result_ok)
        # vpc done but eks still running — should NOT post completion reply
        for call in handler._client.chat_postMessage.call_args_list:
            assert call.kwargs.get("thread_ts") is None

    def test_bad_template_does_not_raise(self):
        """A template with unknown keys must not propagate an exception."""
        handler = self._make_handler(thread_reply_text="Run {bad_key} done")
        defn = self._make_definition()
        result = TerraformResult(0, b"ok", b"")
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", defn, "/tmp")
        # Must not raise despite bad template key
        handler.execute(TerraformAction.PLAN, TerraformStage.POST, "prod", defn, "/tmp", result)
        # A completion reply should still have been posted (with fallback text)
        assert handler._client.chat_postMessage.call_count == 2


class TestSlackHandlerRegistry:
    def test_registered_as_slack(self):
        from tfworker.handlers.registry import HandlerRegistry
        import tfworker.handlers.slack  # noqa: F401
        handler_cls = HandlerRegistry.get_handler("slack")
        from tfworker.handlers.slack import SlackHandler
        assert handler_cls is SlackHandler

    def test_config_model_is_slack_config(self):
        from tfworker.handlers.registry import HandlerRegistry
        import tfworker.handlers.slack  # noqa: F401
        from tfworker.handlers.slack import SlackConfig
        model = HandlerRegistry.get_handler_config_model("slack")
        assert model is SlackConfig
