"""Slack handler for terraform-worker.

Posts a single live-updating status board message to a Slack channel,
updating it in-place as definitions progress through terraform actions.

Example configuration::

    handlers:
      slack:
        channel: "#terraform-runs"
        token: "xoxb-..."          # raw value; supports jinja injection
        # token_env: "SLACK_BOT_TOKEN"  # env var name (default)
        title: "Prod deployment"   # optional; falls back to deployment name
        thread_reply: false        # post summary/error replies in thread
        thread_reply_text: |      # optional template: {run_id} {status} {deployment}
          "Run {run_id} finished: {status}"
"""

import os
import subprocess
from typing import TYPE_CHECKING, Union

import click
from pydantic import BaseModel, PrivateAttr, model_validator
from slack_sdk import WebClient

import tfworker.util.log as log
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from .base import BaseHandler
from .registry import HandlerRegistry

if TYPE_CHECKING:
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition


class SlackConfig(BaseModel):
    channel: str
    token: str | None = None
    token_env: str = "SLACK_BOT_TOKEN"
    title: str | None = None
    thread_reply: bool = False
    thread_reply_text: str | None = None

    _resolved_token: str = PrivateAttr(default="")

    @model_validator(mode="after")
    def resolve_token(self) -> "SlackConfig":
        if self.token:
            self._resolved_token = self.token
            return self
        env_token = os.environ.get(self.token_env)
        if not env_token:
            raise ValueError(
                f"Slack token not found: set env var '{self.token_env}' "
                "or provide 'token' in handler config"
            )
        self._resolved_token = env_token
        return self

    @property
    def resolved_token(self) -> str:
        return self._resolved_token


class SlackStatusBoard:
    """Internal state machine for the Slack status board message."""

    STATUS_EMOJI: dict[str, str] = {
        "pending": "⏳",
        "running": "🔄",
        "done": "✅",
        "changes": "🔵",
        "failed": "❌",
        "skipped": "⏭️",
    }
    ACTION_ORDER: list[str] = ["init", "plan", "apply", "destroy"]

    def __init__(self, channel: str, title: str | None, run_id: str | None):
        self._ts: str | None = None
        self._channel = channel
        self._statuses: dict[str, dict[str, str]] = {}
        self._seen_actions: list[str] = []
        self._deployment: str | None = None
        self._run_id = run_id
        self._title = title
        self._git_context: str | None = None

    def ensure_definition(
        self, definition_name: str, deployment: str, working_dir: str
    ) -> None:
        """Register a definition and capture run context on first call."""
        if self._deployment is None:
            self._deployment = deployment
            self._git_context = self._resolve_git_context(working_dir)
        if definition_name not in self._statuses:
            self._statuses[definition_name] = {}

    def mark(
        self, definition_name: str, action: TerraformAction, status: str
    ) -> None:
        """Update the status of a definition+action pair."""
        action_val = action.value
        if action_val not in self._seen_actions:
            self._seen_actions = [
                a
                for a in self.ACTION_ORDER
                if a in self._seen_actions + [action_val]
            ]
        if definition_name not in self._statuses:
            self._statuses[definition_name] = {}
        self._statuses[definition_name][action_val] = status

    def is_terminal(self) -> bool:
        """Return True when no definition+action remains pending or running."""
        for action_statuses in self._statuses.values():
            for action_val in self._seen_actions:
                if action_statuses.get(action_val, "pending") in ("pending", "running"):
                    return False
        return True

    def overall_status(self) -> str:
        """Return 'in_progress', 'failed', or 'done'."""
        if not self.is_terminal():
            return "in_progress"
        for action_statuses in self._statuses.values():
            if "failed" in action_statuses.values():
                return "failed"
        return "done"

    def failed_count(self) -> int:
        """Return number of definition+action pairs that failed."""
        return sum(
            1
            for action_statuses in self._statuses.values()
            for status in action_statuses.values()
            if status == "failed"
        )

    def _resolve_git_context(self, working_dir: str) -> str | None:
        """Resolve branch and commit from CI env vars or git subprocess."""
        branch = os.environ.get("GITHUB_REF_NAME") or os.environ.get(
            "CI_COMMIT_REF_NAME"
        )
        raw_sha = os.environ.get("GITHUB_SHA") or os.environ.get("CI_COMMIT_SHA") or ""
        commit = raw_sha[:7] if raw_sha else ""

        if not branch:
            try:
                branch = subprocess.check_output(
                    ["git", "branch", "--show-current"],
                    cwd=working_dir,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                ).decode().strip()
            except Exception:
                pass

        if not commit:
            try:
                commit = subprocess.check_output(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=working_dir,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                ).decode().strip()
            except Exception:
                pass

        parts = []
        if branch:
            parts.append(f"Branch: {branch}")
        if commit:
            parts.append(f"Commit: {commit}")
        return "  ".join(parts) if parts else None

    def _build_blocks(self) -> list[dict]:
        """Build Slack Block Kit payload for the current status board state."""
        blocks: list[dict] = []

        # Header block
        title = self._title or self._deployment or "Terraform run"
        header_text = f"🏗️  {title}"
        if self._run_id:
            header_text += f"  |  run: {self._run_id}"
        blocks.append(
            {
                "type": "header",
                "text": {"type": "plain_text", "text": header_text, "emoji": True},
            }
        )

        # Git context block (omitted when unavailable)
        if self._git_context:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": self._git_context}],
                }
            )

        # Status table block
        if self._seen_actions and self._statuses:
            col_headers = ["*Definition*"] + [
                f"*{a.capitalize()}*" for a in self._seen_actions
            ]
            rows = ["\t".join(col_headers)]
            for def_name, action_statuses in self._statuses.items():
                row = [f"`{def_name}`"]
                for action_val in self._seen_actions:
                    status = action_statuses.get(action_val, "pending")
                    row.append(self.STATUS_EMOJI.get(status, "❓"))
                rows.append("\t".join(row))
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "\n".join(rows)},
                }
            )

        # Divider
        blocks.append({"type": "divider"})

        # Overall status banner
        overall = self.overall_status()
        if overall == "in_progress":
            banner = "🟡  *In progress*"
        elif overall == "done":
            banner = "✅  *Run complete — all definitions succeeded*"
        else:
            failed = self.failed_count()
            banner = f"❌  *Run failed — {failed} definition(s) errored*"
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": banner}}
        )

        return blocks

    def post_or_update(self, client: WebClient) -> None:
        """Post the status board as a new message, or update the existing one."""
        blocks = self._build_blocks()
        fallback_text = f"Terraform run: {self._deployment or 'unknown'}"
        try:
            if self._ts is None:
                resp = client.chat_postMessage(
                    channel=self._channel,
                    blocks=blocks,
                    text=fallback_text,
                )
                self._ts = resp["ts"]
                self._channel = resp["channel"]
            else:
                client.chat_update(
                    channel=self._channel,
                    ts=self._ts,
                    blocks=blocks,
                    text=fallback_text,
                )
        except Exception as e:
            log.error(f"Slack API error in post_or_update: {e}")

    def post_thread_reply(self, client: WebClient, text: str) -> None:
        """Post a reply in the thread under the status board message."""
        if self._ts is None:
            return
        try:
            client.chat_postMessage(
                channel=self._channel,
                thread_ts=self._ts,
                text=text,
            )
        except Exception as e:
            log.error(f"Slack API error in post_thread_reply: {e}")


@HandlerRegistry.register("slack")
class SlackHandler(BaseHandler):
    """Post a live-updating Slack status board for each terraform-worker run."""

    actions = [
        TerraformAction.INIT,
        TerraformAction.PLAN,
        TerraformAction.APPLY,
        TerraformAction.DESTROY,
    ]
    config_model = SlackConfig
    _ready = False

    def __init__(self, config: SlackConfig) -> None:
        self.config = config
        self._client = WebClient(token=config.resolved_token)
        run_id = self._get_run_id()
        self._board = SlackStatusBoard(
            channel=config.channel,
            title=config.title,
            run_id=run_id,
        )
        self._ready = True

    def is_ready(self) -> bool:
        return self._ready

    def _get_run_id(self) -> str | None:
        """Retrieve run_id from app state; return None on any failure."""
        try:
            return click.get_current_context().obj.root_options.run_id
        except Exception:
            return None

    def _post_completion_reply(self, deployment: str) -> None:
        overall = self._board.overall_status()
        run_id = self._board._run_id or "unknown"
        if self.config.thread_reply_text:
            try:
                text = self.config.thread_reply_text.format(
                    run_id=run_id,
                    status=overall,
                    deployment=deployment,
                )
            except (KeyError, IndexError) as e:
                log.error(f"Slack thread_reply_text template error: {e}; using default message")
                text = (
                    f"✅ Run complete for `{deployment}` (run: {run_id})"
                    if overall == "done"
                    else f"❌ Run finished with errors for `{deployment}` (run: {run_id})"
                )
        else:
            text = (
                f"✅ Run complete for `{deployment}` (run: {run_id})"
                if overall == "done"
                else f"❌ Run finished with errors for `{deployment}` (run: {run_id})"
            )
        self._board.post_thread_reply(self._client, text)

    def execute(
        self,
        action: "TerraformAction",
        stage: "TerraformStage",
        deployment: str,
        definition: "Definition",
        working_dir: str,
        result: Union["TerraformResult", None] = None,
    ) -> None:
        self._board.ensure_definition(definition.name, deployment, working_dir)

        if stage == TerraformStage.PRE:
            self._board.mark(definition.name, action, "running")
            self._board.post_or_update(self._client)

        elif stage == TerraformStage.POST:
            if result is None:
                status = "failed"
            elif result.exit_code == 0:
                status = "done"
            elif action == TerraformAction.PLAN and result.exit_code == 2:
                status = "changes"
            else:
                status = "failed"
            self._board.mark(definition.name, action, status)
            self._board.post_or_update(self._client)

            if self.config.thread_reply:
                if status == "failed":
                    stderr_snippet = ""
                    if result and result.stderr:
                        stderr_snippet = result.stderr.decode()[:500]
                    else:
                        stderr_snippet = "unknown error"
                    self._board.post_thread_reply(
                        self._client,
                        f"❌ `{definition.name}` {action} failed:\n```{stderr_snippet}```",
                    )
                elif self._board.is_terminal():
                    self._post_completion_reply(deployment)
