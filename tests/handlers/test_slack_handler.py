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
        assert cfg.title is None


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

    def test_skipped_count(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.ensure_definition("eks", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "skipped")
        board.mark("eks", TerraformAction.PLAN, "done")
        assert board.skipped_count() == 1


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

    def test_banner_done_with_skips(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.INIT, "done")
        board.mark("vpc", TerraformAction.PLAN, "skipped")
        blocks = board._build_blocks()
        all_text = str(blocks)
        assert "✅" in all_text
        assert "skipped" in all_text

    def test_banner_done_no_skips(self):
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "done")
        blocks = board._build_blocks()
        all_text = str(blocks)
        assert "succeeded" in all_text
        assert "skipped" not in all_text

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

    def test_status_table_uses_fields_layout(self):
        """Table rows are rendered as section blocks with fields, not tab-separated text."""
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "done")
        blocks = board._build_blocks()
        fields_sections = [b for b in blocks if b.get("type") == "section" and "fields" in b]
        assert len(fields_sections) >= 2  # header row + at least one definition row

    def test_status_table_header_fields_structure(self):
        """Header row has exactly 2 fields: Definition label and action labels."""
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "done")
        blocks = board._build_blocks()
        fields_sections = [b for b in blocks if b.get("type") == "section" and "fields" in b]
        header = fields_sections[0]
        assert len(header["fields"]) == 2
        assert header["fields"][0]["text"] == "*Definition*"
        assert "*Plan*" in header["fields"][1]["text"]

    def test_status_table_definition_row_structure(self):
        """Definition rows have name on left, emoji string on right."""
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "done")
        blocks = board._build_blocks()
        fields_sections = [b for b in blocks if b.get("type") == "section" and "fields" in b]
        def_row = fields_sections[1]
        assert len(def_row["fields"]) == 2
        assert "`vpc`" in def_row["fields"][0]["text"]
        assert "✅" in def_row["fields"][1]["text"]

    def test_multiple_definitions_produce_multiple_rows(self):
        """Multiple definitions produce one section+fields block each."""
        board = self._make_board()
        for name in ["vpc", "eks", "rds"]:
            board.ensure_definition(name, "prod", "/tmp")
            board.mark(name, TerraformAction.PLAN, "done")
        blocks = board._build_blocks()
        fields_sections = [b for b in blocks if b.get("type") == "section" and "fields" in b]
        assert len(fields_sections) == 4  # 1 header + 3 definition rows

    def test_table_section_blocks_have_no_text_key(self):
        """Table section blocks use 'fields', not a top-level 'text' key."""
        board = self._make_board()
        board.ensure_definition("vpc", "prod", "/tmp")
        board.mark("vpc", TerraformAction.PLAN, "done")
        blocks = board._build_blocks()
        for block in blocks:
            if block.get("type") == "section" and "fields" in block:
                assert "text" not in block


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

    def test_plan_exit_code_2_marks_changes(self):
        """Exit code 2 on a plan means changes detected — not an error."""
        handler = self._make_handler()
        defn = self._make_definition()
        result = TerraformResult(2, b"Plan: 1 to add", b"")
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", defn, "/tmp")
        handler.execute(TerraformAction.PLAN, TerraformStage.POST, "prod", defn, "/tmp", result)
        assert handler._board._statuses["vpc"]["plan"] == "changes"

    def test_plan_exit_code_0_marks_done(self):
        """Exit code 0 on a plan means no changes."""
        handler = self._make_handler()
        defn = self._make_definition()
        result = TerraformResult(0, b"No changes.", b"")
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", defn, "/tmp")
        handler.execute(TerraformAction.PLAN, TerraformStage.POST, "prod", defn, "/tmp", result)
        assert handler._board._statuses["vpc"]["plan"] == "done"

    def test_plan_exit_code_1_marks_failed(self):
        """Exit code 1 on a plan means an error."""
        handler = self._make_handler()
        defn = self._make_definition()
        result = TerraformResult(1, b"", b"Error: something broke")
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", defn, "/tmp")
        handler.execute(TerraformAction.PLAN, TerraformStage.POST, "prod", defn, "/tmp", result)
        assert handler._board._statuses["vpc"]["plan"] == "failed"

    def test_exit_code_2_on_non_plan_action_marks_failed(self):
        """Exit code 2 is only meaningful for plan; treat as failure for other actions."""
        handler = self._make_handler()
        defn = self._make_definition()
        result = TerraformResult(2, b"", b"unexpected")
        handler.execute(TerraformAction.APPLY, TerraformStage.PRE, "prod", defn, "/tmp")
        handler.execute(TerraformAction.APPLY, TerraformStage.POST, "prod", defn, "/tmp", result)
        assert handler._board._statuses["vpc"]["apply"] == "failed"

    def test_changes_status_is_terminal(self):
        """A plan with changes should be considered terminal, not stuck pending."""
        handler = self._make_handler()
        defn = self._make_definition()
        result = TerraformResult(2, b"Plan: 1 to add", b"")
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", defn, "/tmp")
        handler.execute(TerraformAction.PLAN, TerraformStage.POST, "prod", defn, "/tmp", result)
        assert handler._board.is_terminal() is True

    def test_changes_status_does_not_count_as_failed(self):
        """overall_status should be 'done' when all plans show changes, not 'failed'."""
        handler = self._make_handler()
        defn = self._make_definition()
        result = TerraformResult(2, b"Plan: 1 to add", b"")
        handler.execute(TerraformAction.PLAN, TerraformStage.PRE, "prod", defn, "/tmp")
        handler.execute(TerraformAction.PLAN, TerraformStage.POST, "prod", defn, "/tmp", result)
        assert handler._board.overall_status() == "done"


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


class TestSlackHandlerSetup:
    def _make_handler(self):
        from tfworker.handlers.slack import SlackConfig, SlackHandler
        cfg = SlackConfig(channel="#ops", token="xoxb-test")
        with patch("tfworker.handlers.slack.WebClient"):
            h = SlackHandler(cfg)
        h._client = MagicMock()
        h._client.chat_postMessage.return_value = {"ts": "1.2", "channel": "C1"}
        return h

    def _defs(self, names=("vpc", "eks"), always_apply=False):
        objs = [Definition(name=n, path="/tmp", always_apply=always_apply) for n in names]
        m = MagicMock()
        m.values.return_value = objs
        return m

    def _opts(self, plan=True, apply=False, destroy=False, plan_destroy=False):
        o = MagicMock()
        o.plan = plan
        o.apply = apply
        o.destroy = destroy
        o.plan_destroy = plan_destroy
        return o

    def test_registers_all_definitions(self):
        h = self._make_handler()
        h.setup("prod", self._defs(["vpc", "eks"]), "/tmp", self._opts())
        assert "vpc" in h._board._statuses
        assert "eks" in h._board._statuses

    def test_seeds_pending_for_plan_action(self):
        h = self._make_handler()
        h.setup("prod", self._defs(["vpc"]), "/tmp", self._opts(plan=True))
        assert h._board._statuses["vpc"].get("plan") == "pending"

    def test_init_always_included(self):
        h = self._make_handler()
        h.setup("prod", self._defs(["vpc"]), "/tmp", self._opts(plan=False))
        assert "init" in h._board._seen_actions

    def test_apply_inferred_from_option(self):
        h = self._make_handler()
        h.setup("prod", self._defs(["vpc"]), "/tmp", self._opts(apply=True))
        assert "apply" in h._board._seen_actions

    def test_apply_inferred_from_always_apply_definition(self):
        h = self._make_handler()
        h.setup("prod", self._defs(["vpc"], always_apply=True), "/tmp", self._opts(apply=False))
        assert "apply" in h._board._seen_actions

    def test_apply_not_included_when_not_requested(self):
        h = self._make_handler()
        h.setup("prod", self._defs(["vpc"]), "/tmp", self._opts(plan=True, apply=False))
        assert "apply" not in h._board._seen_actions

    def test_destroy_inferred_from_option(self):
        h = self._make_handler()
        h.setup("prod", self._defs(["vpc"]), "/tmp", self._opts(destroy=True))
        assert "destroy" in h._board._seen_actions

    def test_posts_initial_board(self):
        h = self._make_handler()
        h.setup("prod", self._defs(["vpc"]), "/tmp", self._opts())
        h._client.chat_postMessage.assert_called_once()

    def test_slack_error_does_not_raise(self):
        h = self._make_handler()
        h._client.chat_postMessage.side_effect = Exception("down")
        h.setup("prod", self._defs(["vpc"]), "/tmp", self._opts())

    def test_does_not_overwrite_existing_status(self):
        """If execute() already marked a status, setup() must not reset it."""
        h = self._make_handler()
        h._board._statuses["vpc"] = {"init": "running"}
        h._board._seen_actions = ["init"]
        defs = MagicMock()
        defs.values.return_value = [Definition(name="vpc", path="/tmp")]
        h.setup("prod", defs, "/tmp", self._opts())
        assert h._board._statuses["vpc"]["init"] == "running"

    def test_plan_destroy_infers_plan_column(self):
        h = self._make_handler()
        h.setup("prod", self._defs(["vpc"]), "/tmp", self._opts(plan=False, plan_destroy=True))
        assert "plan" in h._board._seen_actions


class TestSlackHandlerTeardown:
    def _make_handler(self):
        from tfworker.handlers.slack import SlackConfig, SlackHandler
        cfg = SlackConfig(channel="#ops", token="xoxb-test")
        with patch("tfworker.handlers.slack.WebClient"):
            h = SlackHandler(cfg)
        h._client = MagicMock()
        h._client.chat_postMessage.return_value = {"ts": "1.2", "channel": "C1"}
        h._client.chat_update.return_value = {}
        return h

    def test_teardown_posts_final_update(self):
        h = self._make_handler()
        h._board.ensure_definition("vpc", "prod", "/tmp")
        h._board.mark("vpc", TerraformAction.PLAN, "done")
        h._board._ts = "1.2"
        h.teardown("prod", "/tmp")
        h._client.chat_update.assert_called_once()

    def test_teardown_fixes_stuck_running(self):
        """PRE fired but plan was skipped — teardown marks it skipped, not failed.

        A genuine terraform failure calls ctx.exit(1) which raises SystemExit,
        so teardown is never reached for real failures.
        """
        h = self._make_handler()
        h._board.ensure_definition("vpc", "prod", "/tmp")
        h._board._statuses["vpc"]["plan"] = "running"
        h._board._seen_actions = ["plan"]
        h._board._ts = "1.2"
        h.teardown("prod", "/tmp")
        assert h._board._statuses["vpc"]["plan"] == "skipped"

    def test_teardown_no_extra_messages(self):
        """teardown only updates the board — no additional postMessage calls."""
        h = self._make_handler()
        h._board.ensure_definition("vpc", "prod", "/tmp")
        h._board.mark("vpc", TerraformAction.PLAN, "done")
        h._board._ts = "1.2"
        h.teardown("prod", "/tmp")
        h._client.chat_postMessage.assert_not_called()

    def test_teardown_marks_pending_as_skipped(self):
        """Actions pre-populated by setup() but never started are shown as skipped."""
        h = self._make_handler()
        h._board.ensure_definition("vpc", "prod", "/tmp")
        h._board._statuses["vpc"]["plan"] = "done"
        h._board._statuses["vpc"]["apply"] = "pending"
        h._board._seen_actions = ["plan", "apply"]
        h._board._ts = "1.2"
        h.teardown("prod", "/tmp")
        assert h._board._statuses["vpc"]["apply"] == "skipped"

    def test_teardown_slack_error_does_not_raise(self):
        h = self._make_handler()
        h._client.chat_update.side_effect = Exception("down")
        h._board._ts = "1.2"
        h.teardown("prod", "/tmp")
