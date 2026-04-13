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

from pydantic import BaseModel, PrivateAttr, model_validator


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
