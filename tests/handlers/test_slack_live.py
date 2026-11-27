"""Live Slack integration tests for the slack handler.

These exercise every payload shape the handler produces against the real
Slack API (post, update, delete) — ``blocks.validate`` is known to accept
payloads the message APIs reject, so only real calls prove the layout.

Skipped unless SLACK_LIVE_TEST=1, SLACK_BOT_TOKEN and SLACK_TEST_CHANNEL are
set. Messages are posted to SLACK_TEST_CHANNEL and deleted after each test. Run with::

    SLACK_LIVE_TEST=1 SLACK_BOT_TOKEN=xoxb-... SLACK_TEST_CHANNEL=C... uv run pytest \
        tests/handlers/test_slack_live.py -v
"""

import os
from unittest.mock import patch

import pytest

from tfworker.commands.terraform import TerraformResult
from tfworker.custom_types.terraform import TerraformAction
from tfworker.handlers.slack import SlackConfig, SlackStatusBoard

LIVE = (
    os.environ.get("SLACK_LIVE_TEST") == "1"
    and bool(os.environ.get("SLACK_BOT_TOKEN"))
    and bool(os.environ.get("SLACK_TEST_CHANNEL"))
)
CHANNEL = os.environ.get("SLACK_TEST_CHANNEL", "")

pytestmark = pytest.mark.skipif(
    not LIVE,
    reason="live Slack test; set SLACK_LIVE_TEST=1, SLACK_BOT_TOKEN and SLACK_TEST_CHANNEL",
)

APPLY_OK = b"Apply complete! Resources: 3 added, 1 changed, 0 destroyed."
PLAN_CHANGES = b"Plan: 3 to add, 1 to change, 0 to destroy."
PLAN_CLEAN = b"No changes. Infrastructure is up-to-date."
APPLY_ERROR = (
    b"Error: creating EventBus: AccessDenied - not authorized to perform "
    b"sts:AssumeRole on TerraformExecutor"
)


@pytest.fixture
def client():
    from slack_sdk import WebClient

    return WebClient(token=os.environ["SLACK_BOT_TOKEN"])


@pytest.fixture
def slack_errors():
    """Capture handler-swallowed Slack errors so tests can assert none occurred."""
    with patch("tfworker.handlers.slack.log.error") as mock_error:
        yield mock_error


def make_board(run_id: str, **cfg) -> SlackStatusBoard:
    cfg.setdefault("title", "apps/qa (live test)")
    cfg.setdefault("update_interval", 0.0)
    cfg.setdefault(
        "links",
        [
            {"text": "Argo workflow", "url": "https://argo.example/workflows/ops/wf-1"},
            {
                "text": "logs",
                "url": "https://app.datadoghq.com/logs?query=service%3Aterraform-worker",
            },
        ],
    )
    cfg.setdefault(
        "definition_log_url_template",
        "https://app.datadoghq.com/logs?query=service%3Aterraform-worker%20%40definition%3A{definition}",
    )
    config = SlackConfig(channel=CHANNEL, token=os.environ["SLACK_BOT_TOKEN"], **cfg)
    return SlackStatusBoard(config=config, run_id=run_id, backend_plans=True)


def register(board: SlackStatusBoard, names: list[str], apply: bool = True) -> None:
    actions = [TerraformAction.INIT, TerraformAction.PLAN]
    if apply:
        actions.append(TerraformAction.APPLY)
    board.set_expected_actions(actions)
    for name in names:
        board.ensure_definition(name, "apps-qa", "/tmp")


def cleanup(client, board: SlackStatusBoard) -> None:
    for ts in [board._ts, *board._thread_ts]:
        if ts:
            client.chat_delete(channel=board._channel, ts=ts)


def assert_no_slack_errors(mock_error) -> None:
    errors = [str(c.args[0]) for c in mock_error.call_args_list]
    assert not errors, f"handler logged Slack errors: {errors}"


def test_success_lifecycle_131_definitions(client, slack_errors):
    """131 definitions: post → progress updates → success final, 2 messages total."""
    names = [f"defn_{i:03d}" for i in range(131)]
    board = make_board("live-success", update_interval=1.0)
    register(board, names)
    try:
        # initial post: in-progress container + queued rollup, thread table
        board.post_or_update(client, force=True)
        assert board._ts is not None
        assert len(board._thread_ts) == 1

        # mid-run: some finished, some running (named + rollup), some queued
        for name in names[:100]:
            board.mark(name, TerraformAction.INIT, "done")
            board.mark(name, TerraformAction.PLAN, "changes")
            board.record_result(
                name, TerraformAction.PLAN, TerraformResult(2, PLAN_CHANGES, b"")
            )
            board.mark(name, TerraformAction.APPLY, "done")
            board.record_result(
                name, TerraformAction.APPLY, TerraformResult(0, APPLY_OK, b"")
            )
        for name in names[100:106]:
            board.mark(name, TerraformAction.INIT, "done")
            board.mark(name, TerraformAction.PLAN, "changes")
            board.mark(name, TerraformAction.APPLY, "running")
        board.post_or_update(client, force=True)

        # final: everything done, two clean-plan skips
        for name in names[100:129]:
            board.mark(name, TerraformAction.INIT, "done")
            board.mark(name, TerraformAction.PLAN, "changes")
            board.mark(name, TerraformAction.APPLY, "done")
        for name in names[129:]:
            board.mark(name, TerraformAction.INIT, "done")
            board.record_result(
                name, TerraformAction.PLAN, TerraformResult(0, PLAN_CLEAN, b"")
            )
            board.mark(name, TerraformAction.PLAN, "done")
            board.mark(name, TerraformAction.APPLY, "skipped")
        board.post_or_update(client, force=True)

        assert board.overall_status() == "done"
        # the whole run produced exactly two Slack messages
        assert len(board._thread_ts) == 1
        assert_no_slack_errors(slack_errors)
    finally:
        cleanup(client, board)


def test_failed_lifecycle_with_overflow_failures(client, slack_errors):
    """8 failures: error cards cap at 5 mid-run, failed final shows table + remainder."""
    names = [f"defn_{i:02d}" for i in range(20)]
    board = make_board("live-failed")
    register(board, names)
    try:
        board.post_or_update(client, force=True)

        # 8 plan failures with real error snippets, some successes, one running
        for name in names[:8]:
            board.mark(name, TerraformAction.INIT, "done")
            board.mark(name, TerraformAction.PLAN, "failed")
            board.record_result(
                name,
                TerraformAction.PLAN,
                TerraformResult(1, b"", APPLY_ERROR),
                failed=True,
            )
        for name in names[8:14]:
            board.mark(name, TerraformAction.INIT, "done")
            board.mark(name, TerraformAction.PLAN, "changes")
            board.mark(name, TerraformAction.APPLY, "done")
            board.record_result(
                name, TerraformAction.APPLY, TerraformResult(0, APPLY_OK, b"")
            )
        board.mark(names[14], TerraformAction.INIT, "running")
        board.post_or_update(client, force=True)  # in-progress with error cards

        # teardown behavior: unfinished becomes skipped, final failed state
        board.finalize()
        board.post_or_update(client, force=True)

        assert board.overall_status() == "failed"
        assert_no_slack_errors(slack_errors)
    finally:
        cleanup(client, board)


def test_plan_only_run_with_stored_plans(client, slack_errors):
    """Plan-only run: planned verdicts, plans-stored card, no apply column."""
    names = ["vpc", "eks", "rds"]
    board = make_board("live-plan", update_interval=0.0)
    register(board, names, apply=False)
    try:
        board.post_or_update(client, force=True)
        for name in names[:2]:
            board.mark(name, TerraformAction.INIT, "done")
            board.mark(name, TerraformAction.PLAN, "changes")
            board.record_result(
                name, TerraformAction.PLAN, TerraformResult(2, PLAN_CHANGES, b"")
            )
        board.mark(names[2], TerraformAction.INIT, "done")
        board.record_result(
            names[2], TerraformAction.PLAN, TerraformResult(0, PLAN_CLEAN, b"")
        )
        board.mark(names[2], TerraformAction.PLAN, "done")
        board.post_or_update(client, force=True)

        assert board.overall_status() == "done"
        assert_no_slack_errors(slack_errors)
    finally:
        cleanup(client, board)
