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
"""

import os
import subprocess
from typing import TYPE_CHECKING, Union

import click
from pydantic import BaseModel, Field, PrivateAttr, model_validator
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
    token: str | None = Field(default=None, repr=False)
    token_env: str = "SLACK_BOT_TOKEN"
    title: str | None = None

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
        "skipped": "—",
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

    def mark(self, definition_name: str, action: TerraformAction, status: str) -> None:
        """Update the status of a definition+action pair."""
        action_val = action.value
        if action_val not in self._seen_actions:
            self._seen_actions = [
                a for a in self.ACTION_ORDER if a in self._seen_actions + [action_val]
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

    def skipped_count(self) -> int:
        """Return number of definition+action pairs that were skipped."""
        return sum(
            1
            for action_statuses in self._statuses.values()
            for status in action_statuses.values()
            if status == "skipped"
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
                branch = (
                    subprocess.check_output(
                        ["git", "branch", "--show-current"],
                        cwd=working_dir,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                    .decode()
                    .strip()
                )
            except Exception:
                pass

        if not commit:
            try:
                commit = (
                    subprocess.check_output(
                        ["git", "rev-parse", "--short", "HEAD"],
                        cwd=working_dir,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                    .decode()
                    .strip()
                )
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

        # Status table using 2-column fields layout (left = name, right = emoji row)
        if self._seen_actions and self._statuses:
            action_labels = "  ".join(f"*{a.capitalize()}*" for a in self._seen_actions)
            blocks.append(
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": "*Definition*"},
                        {"type": "mrkdwn", "text": action_labels},
                    ],
                }
            )
            for def_name, action_statuses in self._statuses.items():
                emoji_row = "  ".join(
                    self.STATUS_EMOJI.get(
                        action_statuses.get(action_val, "pending"), "❓"
                    )
                    for action_val in self._seen_actions
                )
                blocks.append(
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"`{def_name}`"},
                            {"type": "mrkdwn", "text": emoji_row},
                        ],
                    }
                )

        # Divider
        blocks.append({"type": "divider"})

        # Overall status banner
        overall = self.overall_status()
        if overall == "in_progress":
            banner = "🟡  *In progress*"
        elif overall == "done":
            skipped = self.skipped_count()
            if skipped:
                banner = f"✅  *Run complete — {skipped} action(s) skipped*"
            else:
                banner = "✅  *Run complete — all definitions succeeded*"
        else:
            failed = self.failed_count()
            banner = f"❌  *Run failed — {failed} action(s) errored*"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": banner}})

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

    def setup(
        self,
        deployment: str,
        definitions,
        working_dir: str,
        terraform_options,
    ) -> None:
        """Pre-populate the board with all definitions and infer expected action columns."""
        try:
            expected_actions = [TerraformAction.INIT]
            if getattr(terraform_options, "plan", False) or getattr(
                terraform_options, "plan_destroy", False
            ):
                expected_actions.append(TerraformAction.PLAN)
            if getattr(terraform_options, "apply", False) or any(
                getattr(defn, "always_apply", False) for defn in definitions.values()
            ):
                expected_actions.append(TerraformAction.APPLY)
            if getattr(terraform_options, "destroy", False):
                expected_actions.append(TerraformAction.DESTROY)

            for defn in definitions.values():
                self._board.ensure_definition(defn.name, deployment, working_dir)
                for action in expected_actions:
                    action_val = action.value
                    if action_val not in self._board._statuses[defn.name]:
                        self._board.mark(defn.name, action, "pending")

            self._board.post_or_update(self._client)
        except Exception as e:
            log.error(f"SlackHandler.setup error: {e}")

    def teardown(
        self,
        deployment: str,
        working_dir: str,
    ) -> None:
        """Finalize the board; resolve any unfinished statuses."""
        try:
            for action_statuses in self._board._statuses.values():
                for action_val in list(action_statuses):
                    if action_statuses[action_val] in ("running", "pending"):
                        action_statuses[action_val] = "skipped"

            self._board.post_or_update(self._client)
        except Exception as e:
            log.error(f"SlackHandler.teardown error: {e}")

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
