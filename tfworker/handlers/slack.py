"""Slack handler for terraform-worker.

Posts a single live-updating status board message to a Slack channel,
updating it in-place as definitions progress through terraform actions.

Example configuration::

    handlers:
      - name: slack
        options:
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

from pydantic import BaseModel, PrivateAttr, model_validator

from tfworker.custom_types.terraform import TerraformAction, TerraformStage


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
        """Placeholder — implemented in Task 5."""
        return []

    def post_or_update(self, client) -> None:
        """Placeholder — implemented in Task 6."""
        pass

    def post_thread_reply(self, client, text: str) -> None:
        """Placeholder — implemented in Task 6."""
        pass
