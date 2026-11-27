import os
import re
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError

from tfworker.commands.terraform import TerraformResult
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.handlers.slack import SlackConfig, SlackStatusBoard


def make_config(**kwargs) -> SlackConfig:
    kwargs.setdefault("channel", "#terraform")
    kwargs.setdefault("token", "xoxb-test")
    kwargs.setdefault("update_interval", 0.0)
    return SlackConfig(**kwargs)


def make_board(run_id="1234", backend_plans=False, **cfg_kwargs) -> SlackStatusBoard:
    return SlackStatusBoard(
        config=make_config(**cfg_kwargs), run_id=run_id, backend_plans=backend_plans
    )


def make_client() -> MagicMock:
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "111.222", "channel": "C123"}
    client.chat_update.return_value = {"ok": True}
    return client


def register(board: SlackStatusBoard, *names: str) -> None:
    board.set_expected_actions(
        [TerraformAction.INIT, TerraformAction.PLAN, TerraformAction.APPLY]
    )
    for name in names:
        board.ensure_definition(name, "apps-qa", "/tmp")


def finish_ok(board: SlackStatusBoard, name: str, changes: bool = True) -> None:
    board.mark(name, TerraformAction.INIT, "done")
    board.mark(name, TerraformAction.PLAN, "changes" if changes else "done")
    board.mark(name, TerraformAction.APPLY, "done" if changes else "skipped")


def fail_def(board: SlackStatusBoard, name: str, action=TerraformAction.APPLY) -> None:
    board.mark(name, TerraformAction.INIT, "done")
    if action != TerraformAction.INIT:
        board.mark(name, TerraformAction.PLAN, "changes")
    board.mark(name, action, "failed")
    result = TerraformResult(1, b"", b"Error: something broke badly\nmore context")
    board.record_result(name, action, result, failed=True)
    # resolve remaining actions so the definition is terminal
    for a in ("init", "plan", "apply"):
        if a not in board._records[name].statuses:
            board._records[name].statuses[a] = "skipped"


def rich_text(block: dict) -> str:
    return block["elements"][0]["elements"][0]["text"]


class TestSlackConfig:
    def test_token_resolution_order(self):
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-env"}):
            assert SlackConfig(channel="#ops").resolved_token == "xoxb-env"
            cfg = SlackConfig(channel="#ops", token="xoxb-raw")
            assert cfg.resolved_token == "xoxb-raw"
        with patch.dict(os.environ, {"MY_TOKEN": "xoxb-custom"}):
            cfg = SlackConfig(channel="#ops", token_env="MY_TOKEN")
            assert cfg.resolved_token == "xoxb-custom"

    def test_missing_token_or_channel_raises(self):
        env = {k: v for k, v in os.environ.items() if k != "SLACK_BOT_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception):
                SlackConfig(channel="#ops")
        with pytest.raises(Exception):
            SlackConfig(token="xoxb-x")

    def test_defaults_and_links(self):
        cfg = SlackConfig(
            channel="#ops",
            token="xoxb-x",
            links=[{"text": "Argo", "url": "https://argo.example/wf/1"}],
        )
        assert cfg.title is None
        assert cfg.definition_log_url_template is None
        assert cfg.update_interval == 4.0
        assert cfg.timezone == "America/Chicago"
        assert cfg.links[0].url == "https://argo.example/wf/1"

    def test_token_not_exposed_in_repr(self):
        assert "xoxb-secret" not in repr(SlackConfig(channel="#o", token="xoxb-secret"))


class TestBoardState:
    def test_expected_actions_ordered_and_primary_derived(self):
        board = make_board()
        board.set_expected_actions(
            [TerraformAction.APPLY, TerraformAction.INIT, TerraformAction.PLAN]
        )
        assert board._expected_actions == ["init", "plan", "apply"]
        assert board._primary_action == "apply"
        board.set_expected_actions([TerraformAction.INIT, TerraformAction.DESTROY])
        assert board._primary_action == "destroy"

    def test_classification_buckets(self):
        board = make_board()
        register(board, "queued", "running", "ok", "no_changes", "failed", "skipped")
        board.mark("running", TerraformAction.INIT, "running")
        finish_ok(board, "ok")
        finish_ok(board, "no_changes", changes=False)
        fail_def(board, "failed")
        for action in ("init", "plan", "apply"):
            board._records["skipped"].statuses[action] = "skipped"

        buckets = board._buckets()
        for bucket in ("queued", "running", "ok", "no_changes", "failed", "skipped"):
            assert [r.name for r in buckets[bucket]] == [bucket]

    def test_aborted_definition_is_skipped_not_no_changes(self):
        # init ran but plan never did (aborted run resolved by teardown):
        # the plan didn't find "no changes" — it didn't run
        board = make_board()
        register(board, "aborted")
        board.mark("aborted", TerraformAction.INIT, "done")
        board._records["aborted"].statuses["plan"] = "skipped"
        board._records["aborted"].statuses["apply"] = "skipped"
        assert board._classify(board._records["aborted"]) == "skipped"

    def test_overall_status_transitions(self):
        board = make_board()
        register(board, "a", "b")
        assert board.overall_status() == "in_progress"
        finish_ok(board, "a")
        assert board.overall_status() == "in_progress"
        finish_ok(board, "b")
        assert board.overall_status() == "done"
        fail_def(board, "a")
        assert board.overall_status() == "failed"

    def test_changes_status_is_terminal_and_not_failed(self):
        board = make_board()
        board.set_expected_actions([TerraformAction.INIT, TerraformAction.PLAN])
        board.ensure_definition("a", "apps-qa", "/tmp")
        board.mark("a", TerraformAction.INIT, "done")
        board.mark("a", TerraformAction.PLAN, "changes")
        assert board.overall_status() == "done"

    def test_mark_tracks_definition_duration(self):
        board = make_board()
        register(board, "vpc")
        board.mark("vpc", TerraformAction.INIT, "running")
        assert board._records["vpc"].started_at is not None
        finish_ok(board, "vpc")
        assert board._records["vpc"].duration_secs() is not None


class TestResultParsing:
    def test_plan_and_apply_output_parsed(self):
        board = make_board()
        register(board, "vpc", "eks", "rds")
        board.record_result(
            "vpc",
            TerraformAction.PLAN,
            TerraformResult(
                2, b"...\nPlan: 3 to add, 12 to change, 1 to destroy.\n", b""
            ),
        )
        board.record_result(
            "eks",
            TerraformAction.PLAN,
            TerraformResult(0, b"No changes. Infrastructure is up-to-date.", b""),
        )
        board.record_result(
            "rds",
            TerraformAction.APPLY,
            TerraformResult(
                0, b"Apply complete! Resources: 3 added, 12 changed, 1 destroyed.", b""
            ),
        )
        assert board._records["vpc"].planned_changes == 16
        assert (
            board._records["vpc"].plan_line
            == "Plan: 3 to add, 12 to change, 1 to destroy"
        )
        assert board._records["eks"].planned_changes == 0
        assert board._records["rds"].applied_changes == 16

    def test_error_snippet_extracted_and_ansi_stripped(self):
        board = make_board()
        register(board, "vpc", "eks")
        board.record_result(
            "vpc",
            TerraformAction.APPLY,
            TerraformResult(
                1, b"", b"\x1b[31mError: creating EventBus: AccessDenied\x1b[0m\ndetail"
            ),
            failed=True,
        )
        board.record_result(
            "eks",
            TerraformAction.PLAN,
            TerraformResult(1, b"", b"something\nwent wrong"),
            failed=True,
        )
        rec = board._records["vpc"]
        assert rec.error_action == "apply"
        assert "AccessDenied" in rec.error_snippet
        assert "\x1b" not in rec.error_snippet
        # no "Error:" line falls back to the tail of the output
        assert "went wrong" in board._records["eks"].error_snippet

    def test_none_result_is_ignored(self):
        board = make_board()
        register(board, "vpc")
        board.record_result("vpc", TerraformAction.APPLY, None, failed=True)
        assert board._records["vpc"].error_snippet is None

    def test_non_utf8_output_does_not_raise(self):
        board = make_board()
        register(board, "vpc")
        board.record_result(
            "vpc",
            TerraformAction.APPLY,
            TerraformResult(1, b"\xff\xfe bad bytes", b"Error: broken \xff"),
            failed=True,
        )
        assert "broken" in board._records["vpc"].error_snippet


class TestGitContext:
    def test_ci_env_vars(self):
        board = make_board()
        env = {"GITHUB_REF_NAME": "main", "GITHUB_SHA": "abcdef1234567890"}
        with patch.dict(os.environ, env):
            board.ensure_definition("vpc", "apps-qa", "/tmp")
        assert board._branch == "main"
        assert board._commit == "abcdef1"

    def test_git_failure_leaves_none(self):
        board = make_board()
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                "GITHUB_REF_NAME",
                "GITHUB_SHA",
                "CI_COMMIT_REF_NAME",
                "CI_COMMIT_SHA",
            )
        }
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "tfworker.handlers.slack.subprocess.check_output",
                side_effect=OSError("no git"),
            ):
                board.ensure_definition("vpc", "apps-qa", "/tmp")
        assert board._branch is None
        assert board._commit is None


class TestMainBlocks:
    def test_in_progress_layout(self):
        board = make_board(
            title="apps/qa",
            links=[{"text": "Argo workflow", "url": "https://argo.example/wf"}],
        )
        register(board, *[f"def{i}" for i in range(131)])
        finish_ok(board, "def0")
        board.mark("def1", TerraformAction.APPLY, "running")
        board.mark("def2", TerraformAction.APPLY, "running")

        blocks = board._build_main_blocks()
        assert [b["type"] for b in blocks] == ["container", "plan"]

        container = blocks[0]
        assert container["title"]["text"] == "Apply — apps/qa"
        assert container["has_header_divider"] is True
        assert "is_collapsible" not in container
        subtitle = container["subtitle"]["text"]
        assert "run `1234`" in subtitle
        # plain clock in the configured timezone: the container subtitle
        # renders Slack's <!date> command literally rather than processing it
        assert "<!date" not in subtitle
        assert re.search(r"started \d\d:\d\d C[DS]T", subtitle)

        status, counts, links = container["child_blocks"]
        assert "*Applying*" in status["text"]["text"]
        assert "2 definitions running" in status["text"]["text"]
        assert "1 of 131 finished" in status["text"]["text"]
        counts_text = " ".join(e["text"] for e in counts["elements"])
        assert "*131* definitions" in counts_text
        assert "*1* applied" in counts_text
        assert "*2* running" in counts_text
        assert "*128* queued" in counts_text
        assert "<https://argo.example/wf|Argo workflow>" in links["elements"][0]["text"]
        # live relative timestamp in the links context (which renders <!date>)
        updated = links["elements"][1]["text"]
        assert re.search(r"updated <!date\^\d+\^\{ago\}\|\d\d:\d\d C[DS]T>", updated)

    def test_success_final_layout(self):
        board = make_board()
        register(board, "a", "b")
        finish_ok(board, "a")
        finish_ok(board, "b", changes=False)

        container, plan = board._build_main_blocks()
        assert container["is_collapsible"] is True
        assert "has_header_divider" not in container
        assert "finished in" in container["subtitle"]["text"]
        verdict = container["child_blocks"][0]["text"]["text"]
        assert "Run complete — 1 applied, 1 no changes" in verdict

        assert all(t["status"] == "complete" for t in plan["tasks"])
        no_changes = next(
            t for t in plan["tasks"] if t["task_id"] == "rollup_no_changes"
        )
        assert "1 definitions with no changes" in no_changes["title"]
        assert not any(t["task_id"] == "rollup_skipped" for t in plan["tasks"])

    def test_failed_final_layout(self):
        board = make_board()
        register(board, *[f"def{i}" for i in range(9)])
        finish_ok(board, "def8")
        for i in range(8):
            fail_def(board, f"def{i}")

        blocks = board._build_main_blocks()
        # the plan block is dropped in favor of the container's failure table
        assert len(blocks) == 1
        container = blocks[0]
        verdict = container["child_blocks"][0]["text"]["text"]
        assert "Run failed — 8 of 9 definitions errored" in verdict

        table = next(c for c in container["child_blocks"] if c["type"] == "table")
        assert len(table["rows"]) == 1 + board.MAX_FAILURE_TABLE_ROWS
        assert table["rows"][1][1]["text"] == "apply"
        assert len(table["rows"][1][2]["text"]) <= board.FAILURE_ERROR_CHARS
        more = container["child_blocks"][-1]["elements"][0]["text"]
        assert "*3 more* failures" in more


class TestPlanBlock:
    def test_stable_block_id_across_updates(self):
        # a fresh block_id per update would reset the block's
        # expanded/collapsed state under a watching user
        board = make_board()
        register(board, "a")
        first = board._build_main_blocks()[1]["block_id"]
        board.mark("a", TerraformAction.INIT, "running")
        assert board._build_main_blocks()[1]["block_id"] == first

    def test_error_cards_capped_with_datadog_sources(self):
        # the literal braces in the template must not raise (str.format would)
        board = make_board(
            definition_log_url_template='https://dd.example/logs?q={"svc"}&def={definition}'
        )
        register(board, *[f"def{i}" for i in range(10)])
        for i in range(8):
            fail_def(board, f"def{i}")
        board.mark("def8", TerraformAction.INIT, "running")

        plan = board._build_main_blocks()[1]
        errors = [t for t in plan["tasks"] if t["status"] == "error"]
        assert len(errors) == board.MAX_ERROR_CARDS
        assert (
            errors[0]["sources"][0]["url"]
            == 'https://dd.example/logs?q={"svc"}&def=def0'
        )

    def test_running_tasks_named_oldest_first_then_rolled_up(self):
        board = make_board()
        register(board, *[f"def{i:02d}" for i in range(14)])
        for i in range(12):
            board.mark(f"def{i:02d}", TerraformAction.INIT, "running")
        board._records["def03"].started_at = 0.5  # oldest

        plan = board._build_main_blocks()[1]
        assert plan["title"] == "📝 Run Details"
        named = [t for t in plan["tasks"] if t["task_id"].startswith("run_")]
        assert len(named) == board.MAX_NAMED_RUNNING
        assert named[0]["task_id"] == "run_def03"
        # verbs follow the action actually running (init here), not the
        # run's primary verb ("applying")
        assert named[0]["title"].endswith("— initializing")
        rollup = next(t for t in plan["tasks"] if t["task_id"] == "rollup_running")
        assert "2 more initializing" in rollup["title"]
        queued = next(t for t in plan["tasks"] if t["task_id"] == "rollup_queued")
        assert "2 definitions queued" in queued["title"]

    def test_no_changes_rollup_shown_mid_run(self):
        board = make_board()
        register(board, "clean", "busy")
        finish_ok(board, "clean", changes=False)
        board.mark("busy", TerraformAction.INIT, "running")
        plan = board._build_main_blocks()[1]
        no_changes = next(
            t for t in plan["tasks"] if t["task_id"] == "rollup_no_changes"
        )
        assert "1 definitions with no changes" in no_changes["title"]

    def test_running_verb_follows_actual_action(self):
        board = make_board()
        register(board, "a", "b")
        board.mark("a", TerraformAction.INIT, "running")
        status = board._build_main_blocks()[0]["child_blocks"][0]["text"]["text"]
        assert "*Initializing*" in status
        board.mark("a", TerraformAction.INIT, "done")
        board.mark("a", TerraformAction.APPLY, "running")
        status = board._build_main_blocks()[0]["child_blocks"][0]["text"]["text"]
        assert "*Applying*" in status

    def test_running_task_shows_plan_line(self):
        board = make_board()
        register(board, "a")
        board.record_result(
            "a",
            TerraformAction.PLAN,
            TerraformResult(2, b"Plan: 3 to add, 0 to change, 0 to destroy.", b""),
        )
        board.mark("a", TerraformAction.APPLY, "running")
        card = next(
            t for t in board._build_main_blocks()[1]["tasks"] if t["task_id"] == "run_a"
        )
        assert "3 to add" in rich_text(card["details"])

    def test_complete_rollup_aggregates_resources(self):
        board = make_board()
        register(board, "a", "b")
        finish_ok(board, "a")
        board.record_result(
            "a",
            TerraformAction.APPLY,
            TerraformResult(
                0, b"Apply complete! Resources: 2 added, 3 changed, 0 destroyed.", b""
            ),
        )
        board.mark("b", TerraformAction.INIT, "running")
        rollup = next(
            t
            for t in board._build_main_blocks()[1]["tasks"]
            if t["task_id"] == "rollup_complete"
        )
        assert "1 definitions applied" in rollup["title"]
        assert "5 resources changed" in rich_text(rollup["output"])

    def test_plans_stored_card_only_for_backend_plan_runs(self):
        board = make_board(run_id="42", backend_plans=True)
        board.set_expected_actions([TerraformAction.INIT, TerraformAction.PLAN])
        board.ensure_definition("a", "apps-qa", "/tmp")
        board.mark("a", TerraformAction.INIT, "done")
        board.mark("a", TerraformAction.PLAN, "changes")
        board.record_result(
            "a",
            TerraformAction.PLAN,
            TerraformResult(2, b"Plan: 1 to add, 0 to change, 0 to destroy.", b""),
        )
        plan = board._build_main_blocks()[1]
        stored = next(t for t in plan["tasks"] if t["task_id"] == "rollup_plans")
        assert "run 42" in rich_text(stored["output"])

        apply_board = make_board(backend_plans=True)
        register(apply_board, "a")
        finish_ok(apply_board, "a")
        tasks = apply_board._build_main_blocks()[1]["tasks"]
        assert not any(t["task_id"] == "rollup_plans" for t in tasks)


class TestThreadMessages:
    def test_table_layout_and_cell_types(self):
        board = make_board()
        register(board, "vpc")
        board.mark("vpc", TerraformAction.INIT, "done")
        board.mark("vpc", TerraformAction.PLAN, "failed")
        board.mark("vpc", TerraformAction.APPLY, "skipped")

        heading, table = board._build_thread_messages()[0]
        assert "Per-definition results" in heading["text"]["text"]
        assert table["type"] == "data_table"
        assert [c["text"] for c in table["rows"][0]] == [
            "Definition",
            "Init",
            "Plan",
            "Apply",
            "Changed",
            "Ran (s)",
        ]
        row = table["rows"][1]
        assert rich_text(row[0]) == "vpc"
        assert row[1]["elements"][0]["elements"][0]["name"] == "white_check_mark"
        assert row[2]["elements"][0]["elements"][0]["name"] == "x"
        # skipped and unknown numerics render as raw_text em dashes
        assert row[3] == {"type": "raw_text", "text": "—"}
        assert row[4] == {"type": "raw_text", "text": "—"}

    def test_numeric_cells_are_raw_text(self):
        board = make_board()
        register(board, "a")
        finish_ok(board, "a")
        board.record_result(
            "a",
            TerraformAction.APPLY,
            TerraformResult(
                0, b"Apply complete! Resources: 5 added, 0 changed, 0 destroyed.", b""
            ),
        )
        row = board._build_thread_messages()[0][1]["rows"][1]
        assert row[4] == {"type": "raw_text", "text": "5"}
        assert row[5]["type"] == "raw_text"

    def test_chunking(self):
        board = make_board()
        register(board, *[f"def{i}" for i in range(131)])
        messages = board._build_thread_messages()
        assert len(messages) == 1
        assert len(messages[0][1]["rows"]) == 132  # header + 131 rows

        register(board, *[f"extra{i}" for i in range(119)])  # 250 total
        messages = board._build_thread_messages()
        assert len(messages) == 2
        assert len(messages[0][1]["rows"]) == 201
        assert len(messages[1][0]["rows"]) == 51
        assert "part 2/2" in messages[1][0]["caption"]

    def test_chunking_by_character_budget(self):
        board = make_board()
        register(board, *["x" * 400 + str(i) for i in range(100)])
        messages = board._build_thread_messages()
        # 100 rows fit the row limit but blow the 18k char budget
        assert len(messages) > 1
        for blocks in messages:
            table = blocks[-1]
            chars = sum(
                board._cell_chars(cell) for row in table["rows"] for cell in row
            )
            assert chars <= board.MAX_DATA_TABLE_CHARS + 500  # header overhead


class TestFallbackText:
    def test_covers_each_state(self):
        board = make_board(title="apps/qa")
        register(board, "a", "b")
        finish_ok(board, "a")
        assert "Apply apps/qa: in progress — 1 of 2 finished" in board._fallback_text()
        finish_ok(board, "b")
        assert "complete — 2 applied" in board._fallback_text()
        fail_def(board, "b")
        assert "failed — 1 of 2 definitions errored" in board._fallback_text()


class TestPostOrUpdate:
    def test_posts_then_updates_in_place(self):
        board = make_board()
        register(board, "a")
        client = make_client()
        board.post_or_update(client)
        # first flush posts the channel message and one threaded reply
        assert client.chat_postMessage.call_count == 2
        calls = client.chat_postMessage.call_args_list
        assert "thread_ts" not in calls[0].kwargs
        assert calls[1].kwargs["thread_ts"] == "111.222"
        assert all(c.kwargs.get("text") for c in calls)

        board.mark("a", TerraformAction.INIT, "running")
        board.post_or_update(client)
        assert client.chat_postMessage.call_count == 2
        assert client.chat_update.called

    def test_unchanged_state_skips_api_calls(self):
        board = make_board()
        register(board, "a")
        client = make_client()
        board.post_or_update(client)
        board.post_or_update(client)
        assert client.chat_postMessage.call_count == 2
        assert client.chat_update.call_count == 0

    def test_debounce_coalesces_and_force_bypasses(self):
        board = make_board(update_interval=300.0)
        register(board, "a")
        client = make_client()
        board.post_or_update(client, force=True)
        board.mark("a", TerraformAction.INIT, "running")
        board.post_or_update(client)
        assert client.chat_update.call_count == 0
        finish_ok(board, "a")
        board.post_or_update(client, force=True)
        assert client.chat_update.called

    def test_zero_definition_run_still_posts(self):
        client = make_client()
        make_board().post_or_update(client, force=True)
        client.chat_postMessage.assert_called_once()

    def test_slack_error_is_logged_not_raised_and_debounced(self):
        board = make_board(update_interval=300.0)
        register(board, "a")
        client = make_client()
        client.chat_postMessage.side_effect = SlackApiError(
            "boom", MagicMock(status_code=500)
        )
        board.post_or_update(client)  # must not raise
        # a failed post must still engage the debounce, not retry per event
        board.post_or_update(client)
        assert client.chat_postMessage.call_count == 1

    def test_rate_limited_update_is_dropped_and_deferred(self):
        board = make_board()
        register(board, "a")
        client = make_client()
        board.post_or_update(client)
        board.mark("a", TerraformAction.INIT, "running")
        resp = MagicMock(status_code=429)
        resp.headers = {"Retry-After": "30"}
        client.chat_update.side_effect = SlackApiError("ratelimited", resp)
        board.post_or_update(client)  # must not raise
        assert board._next_update_at > time.monotonic()

    def test_forced_call_retries_on_rate_limit(self):
        board = make_board()
        register(board, "a")
        client = make_client()
        resp = MagicMock(status_code=429)
        resp.headers = {"Retry-After": "1"}
        client.chat_postMessage.side_effect = [
            SlackApiError("ratelimited", resp),
            {"ts": "111.222", "channel": "C123"},
            {"ts": "111.333", "channel": "C123"},
        ]
        with patch("tfworker.handlers.slack.time.sleep") as sleep:
            board.post_or_update(client, force=True)
        assert board._ts == "111.222"
        sleep.assert_called()


def make_handler(**cfg_kwargs):
    from tfworker.handlers.slack import SlackHandler

    handler = SlackHandler(make_config(**cfg_kwargs))
    handler._client = make_client()
    return handler


def make_definition(name: str, always_apply: bool = False) -> SimpleNamespace:
    return SimpleNamespace(name=name, always_apply=always_apply)


def make_options(**kwargs) -> SimpleNamespace:
    defaults = {"plan": True, "apply": False, "destroy": False, "plan_destroy": False}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestSlackHandler:
    def test_registered_and_ready(self):
        from tfworker.handlers.registry import HandlerRegistry
        from tfworker.handlers.slack import SlackHandler

        assert HandlerRegistry.get_handlers()["slack"] is SlackHandler
        assert SlackHandler.config_model is SlackConfig
        handler = make_handler()
        assert handler.is_ready() is True
        # outside a click context, root options fall back to defaults
        assert handler._board._run_id is None
        assert handler._board._backend_plans is False

    def test_setup_registers_definitions_and_infers_actions(self):
        handler = make_handler()
        definitions = {n: make_definition(n) for n in ("d1", "d2")}
        handler.setup("apps-qa", definitions, "/tmp", make_options(apply=True))
        assert set(handler._board._records) == {"d1", "d2"}
        assert handler._board._expected_actions == ["init", "plan", "apply"]
        assert handler._board._primary_action == "apply"
        assert handler._client.chat_postMessage.call_count == 2  # main + thread

    @pytest.mark.parametrize(
        "options,definition,expected",
        [
            ({}, make_definition("d"), ["init", "plan"]),
            (
                {"plan": False, "destroy": True},
                make_definition("d"),
                ["init", "destroy"],
            ),
            (
                {"plan": False, "plan_destroy": True},
                make_definition("d"),
                ["init", "plan"],
            ),
        ],
    )
    def test_setup_expected_action_inference(self, options, definition, expected):
        handler = make_handler()
        handler.setup("apps-qa", {"d": definition}, "/tmp", make_options(**options))
        assert handler._board._expected_actions == expected

    def test_always_apply_is_per_definition_not_run_wide(self):
        handler = make_handler()
        definitions = {
            "normal": make_definition("normal"),
            "always": make_definition("always", always_apply=True),
        }
        handler.setup("apps-qa", definitions, "/tmp", make_options())
        board = handler._board

        # one always_apply definition must not turn a plan run into an apply
        assert board._primary_action == "plan"
        assert board._records["normal"].expected == ["init", "plan"]
        assert board._records["always"].expected == ["init", "plan", "apply"]

        # the apply column appears, but only for the always_apply definition
        table = board._build_thread_messages()[0][1]
        assert [c["text"] for c in table["rows"][0]][:4] == [
            "Definition",
            "Init",
            "Plan",
            "Apply",
        ]
        normal_row, always_row = table["rows"][1], table["rows"][2]
        assert normal_row[3] == {"type": "raw_text", "text": "—"}
        assert always_row[3]["type"] == "rich_text"  # pending hourglass

        # a planned-changes definition finishes at plan and counts as planned,
        # not "no changes" from an apply expectation it never had
        board.mark("normal", TerraformAction.INIT, "done")
        board.mark("normal", TerraformAction.PLAN, "changes")
        assert board._classify(board._records["normal"]) == "ok"

    @pytest.mark.parametrize(
        "action,result,expected_status",
        [
            (TerraformAction.INIT, TerraformResult(0, b"", b""), "done"),
            (TerraformAction.APPLY, None, "failed"),
            (TerraformAction.PLAN, TerraformResult(2, b"", b""), "changes"),
            (TerraformAction.APPLY, TerraformResult(2, b"", b""), "failed"),
        ],
    )
    def test_execute_post_status_mapping(self, action, result, expected_status):
        handler = make_handler()
        handler.setup(
            "apps-qa", {"d1": make_definition("d1")}, "/tmp", make_options(apply=True)
        )
        handler.execute(
            action,
            TerraformStage.POST,
            "apps-qa",
            make_definition("d1"),
            "/tmp",
            result=result,
        )
        assert handler._board._records["d1"].statuses[action.value] == expected_status

    def test_execute_pre_marks_running(self):
        handler = make_handler()
        handler.setup(
            "apps-qa", {"d1": make_definition("d1")}, "/tmp", make_options(apply=True)
        )
        handler.execute(
            TerraformAction.INIT,
            TerraformStage.PRE,
            "apps-qa",
            make_definition("d1"),
            "/tmp",
        )
        assert handler._board._records["d1"].statuses["init"] == "running"

    def test_clean_plan_marks_apply_skipped(self):
        handler = make_handler()
        handler.setup(
            "apps-qa", {"d1": make_definition("d1")}, "/tmp", make_options(apply=True)
        )
        handler.execute(
            TerraformAction.PLAN,
            TerraformStage.POST,
            "apps-qa",
            make_definition("d1"),
            "/tmp",
            result=TerraformResult(0, b"No changes.", b""),
        )
        rec = handler._board._records["d1"]
        assert rec.statuses["plan"] == "done"
        assert rec.statuses["apply"] == "skipped"

    def test_error_stage_records_snippet(self):
        handler = make_handler()
        handler.setup(
            "apps-qa", {"d1": make_definition("d1")}, "/tmp", make_options(apply=True)
        )
        handler.execute(
            TerraformAction.APPLY,
            TerraformStage.ERROR,
            "apps-qa",
            make_definition("d1"),
            "/tmp",
            result=TerraformResult(1, b"", b"Error: AccessDenied on sts:AssumeRole"),
        )
        rec = handler._board._records["d1"]
        assert rec.statuses["apply"] == "failed"
        assert "AccessDenied" in rec.error_snippet

    def test_init_error_stage_reports_run_as_failed(self):
        """A definition that never got past prepare/init must not read as a
        clean run: the ERROR stage marks init failed, and teardown resolving
        the rest as skipped leaves the run failed, not complete."""
        handler = make_handler()
        handler.setup(
            "apps-qa",
            {n: make_definition(n) for n in ("d1", "d2")},
            "/tmp",
            make_options(plan=True),
        )
        handler.execute(
            TerraformAction.INIT,
            TerraformStage.ERROR,
            "apps-qa",
            make_definition("d1"),
            "/tmp",
            result=TerraformResult(
                1, b"", b"Error: Failed to download module from git::ssh://old-host"
            ),
        )
        handler.teardown("apps-qa", "/tmp")

        board = handler._board
        assert board._records["d1"].statuses["init"] == "failed"
        assert board._records["d1"].error_action == "init"
        assert "old-host" in board._records["d1"].error_snippet
        assert board.overall_status() == "failed"

    def test_teardown_marks_unfinished_as_skipped_and_flushes(self):
        handler = make_handler()
        handler.setup(
            "apps-qa",
            {n: make_definition(n) for n in ("d1", "d2")},
            "/tmp",
            make_options(apply=True),
        )
        handler.execute(
            TerraformAction.INIT,
            TerraformStage.PRE,
            "apps-qa",
            make_definition("d1"),
            "/tmp",
        )
        handler.teardown("apps-qa", "/tmp")
        board = handler._board
        assert board._records["d1"].statuses["init"] == "skipped"
        assert board._records["d2"].statuses["plan"] == "skipped"
        assert board.is_terminal() is True
        assert handler._client.chat_update.called

    def test_slack_failures_never_raise(self):
        handler = make_handler()
        handler._client.chat_postMessage.side_effect = Exception("boom")
        handler._client.chat_update.side_effect = Exception("boom")
        handler.setup("apps-qa", {"d": make_definition("d")}, "/tmp", make_options())
        handler.execute(
            TerraformAction.INIT,
            TerraformStage.PRE,
            "apps-qa",
            make_definition("d"),
            "/tmp",
        )
        handler.teardown("apps-qa", "/tmp")
