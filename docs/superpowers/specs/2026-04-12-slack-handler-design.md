# Slack Handler Design

**Date:** 2026-04-12
**Branch:** feat/slack_handler
**Status:** Approved

## Overview

Add a Slack handler to terraform-worker that posts a single live-updating status board message to a Slack channel for each run. The message shows per-definition, per-action status (init → plan → apply → destroy) and updates in-place as the run progresses. A final banner reflects overall pass/fail. An optional threaded reply can post error details and a completion summary.

## Architecture

### File Layout

One new file: `tfworker/handlers/slack.py`

No changes to any other source file except `pyproject.toml` (add `slack-sdk` dependency).

The file contains three classes:

1. `SlackConfig` — Pydantic `BaseModel` for handler configuration
2. `SlackStatusBoard` — internal state machine; owns definition/action status tracking and Block Kit payload construction
3. `SlackHandler` — thin `BaseHandler` subclass; delegates all state/rendering to `SlackStatusBoard`, calls Slack API

Registered via `@HandlerRegistry.register("slack")`.

### Dependency

Add `slack-sdk` to `pyproject.toml` dependencies.

## Configuration

```yaml
handlers:
  slack:
    channel: "#terraform-runs"      # required

    # Token resolution — pick one, or none to use default env var.
    # Only one should be set; token takes precedence over token_env.
    token: "xoxb-..."              # raw value; supports jinja template injection
    token_env: "MY_SLACK_TOKEN"    # custom env var name (default: SLACK_BOT_TOKEN)

    # Header (all optional)
    title: "Prod deployment"       # falls back to deployment name

    # Thread reply (optional)
    thread_reply: false            # default false
    thread_reply_text: |           # optional template; vars: {run_id} {status} {deployment}
      "Run {run_id} finished with status: {status}"
```

### `SlackConfig` Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `channel` | `str` | required | Slack channel to post to |
| `token` | `str \| None` | `None` | Raw bot token (supports jinja injection) |
| `token_env` | `str` | `"SLACK_BOT_TOKEN"` | Env var name to read token from |
| `title` | `str \| None` | `None` | Run title; falls back to deployment name |
| `thread_reply` | `bool` | `False` | Post summary/error replies in thread |
| `thread_reply_text` | `str \| None` | `None` | Custom template for completion thread reply |

### Token Resolution

Evaluated once in a `@model_validator` at config parse time, in priority order:

1. `token` field (raw value present in config)
2. `os.environ[token_env]` (reads named env var; default name `SLACK_BOT_TOKEN`)
3. Raises `HandlerError` if neither yields a value

The resolved token is stored internally; it is never exposed back through the config model.

## `SlackStatusBoard` — State & Rendering

### Internal State

| Attribute | Type | Description |
|---|---|---|
| `_ts` | `str \| None` | Slack message `ts`; `None` until first post |
| `_channel` | `str` | Target Slack channel |
| `_thread_ts` | `str \| None` | Set after first thread reply is posted |
| `_statuses` | `dict[str, dict[str, str]]` | `definition → action_value → status_key` |
| `_action_order` | `list[str]` | Ordered list of action values seen so far |
| `_deployment` | `str` | Captured from first execute call |
| `_run_id` | `str \| None` | From `app_state.root_options.run_id` |
| `_title` | `str \| None` | From config |
| `_git_context` | `str \| None` | Best-effort git branch/commit string |

### Status Values

| Key | Emoji | Meaning |
|---|---|---|
| `pending` | ⏳ | Action not yet started |
| `running` | 🔄 | PRE stage in progress |
| `done` | ✅ | POST stage, exit code 0 |
| `failed` | ❌ | POST stage, exit code != 0 |
| `skipped` | ⏭️ | Action explicitly skipped |

### Status Transitions

- `execute(PRE)` → mark definition+action as `running`, call `post_or_update`
- `execute(POST, exit_code=0)` → mark as `done`, call `post_or_update`
- `execute(POST, exit_code!=0)` → mark as `failed`, call `post_or_update`
- Definitions present in `_statuses` but not yet reached for an action show `pending`

### Column Detection

The board tracks which action values it has ever seen across all definitions. Columns are added dynamically as new actions appear. Display order is always: `init → plan → apply → destroy`. A plan-only run shows two columns; a mixed run (some definitions with `always_apply=True`) shows three.

### Block Kit Layout

```
┌─────────────────────────────────────────────┐
│ 🏗️  Prod deployment  |  run: abc-123         │  ← header block
│ Branch: main  Commit: a1b2c3d               │  ← git context (omitted if unavailable)
├──────────────────────────────────────────────┤
│ Definition    │ Init │ Plan │ Apply          │  ← columns for observed actions only
│ vpc           │  ✅  │  ✅  │  🔄           │
│ eks           │  ✅  │  ⏳  │  ⏳           │
│ rds           │  🔄  │  ⏳  │  ⏳           │
├──────────────────────────────────────────────┤
│ 🟡 In progress                              │  ← overall status banner
└─────────────────────────────────────────────┘
```

**Overall banner states:**
- `🟡 In progress` — any definition+action still `pending` or `running`
- `✅ Run complete — all definitions succeeded`
- `❌ Run failed — N definition(s) errored`

### Git Context (Best-Effort)

Checked in order; first match wins; omitted from message if nothing resolves:

1. `GITHUB_REF` / `GITHUB_SHA` (GitHub Actions)
2. `CI_COMMIT_REF_NAME` / `CI_COMMIT_SHA` (GitLab CI)
3. `git branch --show-current` and `git rev-parse --short HEAD` run in `working_dir`

Git failures are silently swallowed; they never affect the run.

## `SlackHandler` — Execute Flow

### Registration

```python
@HandlerRegistry.register("slack")
class SlackHandler(BaseHandler):
    actions = [TerraformAction.INIT, TerraformAction.PLAN,
               TerraformAction.APPLY, TerraformAction.DESTROY]
    config_model = SlackConfig
```

### `__init__`

1. Validate and store config
2. Instantiate `slack_sdk.WebClient(token=resolved_token)`
3. Instantiate `SlackStatusBoard(config)`
4. Set `_ready = True`

### `execute()` Logic

```
execute(action, stage, deployment, definition, working_dir, result):
  if stage == PRE:
    board.ensure_definition(definition.name, deployment, working_dir)
    board.mark(definition.name, action, "running")
    board.post_or_update(client)

  if stage == POST:
    status = "done" if result.exit_code == 0 else "failed"
    board.mark(definition.name, action, status)
    board.post_or_update(client)

    if config.thread_reply:
      if status == "failed":
        post thread reply with error detail (stderr snippet)
      if board.is_terminal():
        post thread reply with completion summary
```

`board.is_terminal()` returns `True` when no definition+action remains in `pending` or `running` state.

### Error Handling

All `slack_sdk` calls are wrapped in `try/except`. Failures are logged via `tfworker.util.log.error()` but never re-raised. A Slack API failure must never abort a terraform run.

### Optional S3 Coordination

If the user wants the status board to reflect S3 state saves, they can set:

```python
dependencies = {
    TerraformAction.APPLY: {
        TerraformStage.POST: ["s3"]
    }
}
```

This uses the existing `BaseHandler.dependencies` mechanism and requires no changes outside `slack.py`. This is not enabled by default.

## Testing

- `tests/handlers/test_slack_handler.py`
- Unit tests for `SlackStatusBoard` in isolation (no Slack API calls): status transitions, column detection, Block Kit payload structure, terminal state detection, git context resolution
- Integration-style tests for `SlackHandler.execute()` with `slack_sdk.WebClient` mocked: verifies `postMessage` on first call, `update` on subsequent calls, thread reply gating
- Config validation tests: token resolution precedence, missing token raises `HandlerError`

## Out of Scope

- Multiple channels per run
- Per-definition channel routing
- Webhook support (webhooks cannot update messages)
- Async/concurrent Slack API calls
