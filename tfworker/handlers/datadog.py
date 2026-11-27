"""Datadog handler for terraform-worker.

Posts a Datadog *change event* to the Events API (v2) after a definition is
successfully applied or destroyed. The event records the current git context
(branch, commit, subject) and attributes the change to the author of the most
recent commit.

Example configuration::

    handlers:
      datadog:
        api_key: "..."              # raw value; supports jinja injection
        app_key: "..."              # raw value; supports jinja injection
        # api_key_env: "DD_API_KEY" # env var name (default)
        # app_key_env: "DD_APP_KEY" # env var name (default)
"""

import json
import os
import subprocess
from typing import TYPE_CHECKING, Union

import urllib3
from pydantic import BaseModel, Field, PrivateAttr, model_validator

import tfworker.util.log as log
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.util.system import strip_ansi

from .base import BaseHandler
from .registry import HandlerRegistry

if TYPE_CHECKING:  # pragma: no cover
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition


class DatadogConfig(BaseModel):
    """Configuration for the Datadog change-event handler."""

    api_key: str | None = Field(default=None, repr=False)
    app_key: str | None = Field(default=None, repr=False)
    api_key_env: str = "DD_API_KEY"
    app_key_env: str = "DD_APP_KEY"

    _resolved_api_key: str = PrivateAttr(default="")
    _resolved_app_key: str = PrivateAttr(default="")

    @model_validator(mode="after")
    def resolve_credentials(self) -> "DatadogConfig":
        if self.api_key:
            self._resolved_api_key = self.api_key
        else:
            env_key = os.environ.get(self.api_key_env)
            if not env_key:
                raise ValueError(
                    f"Datadog API key not found: set env var '{self.api_key_env}' "
                    "or provide 'api_key' in handler config"
                )
            self._resolved_api_key = env_key

        if self.app_key:
            self._resolved_app_key = self.app_key
        else:
            env_key = os.environ.get(self.app_key_env)
            if not env_key:
                raise ValueError(
                    f"Datadog APP key not found: set env var '{self.app_key_env}' "
                    "or provide 'app_key' in handler config"
                )
            self._resolved_app_key = env_key
        return self

    @property
    def resolved_api_key(self) -> str:
        return self._resolved_api_key

    @property
    def resolved_app_key(self) -> str:
        return self._resolved_app_key

    @property
    def events_url(self) -> str:
        return "https://event-management-intake.datadoghq.com/api/v2/events"

    @property
    def validate_url(self) -> str:
        return "https://api.datadoghq.com/api/v1/validate"


@HandlerRegistry.register("datadog")
class DatadogHandler(BaseHandler):
    """Post a Datadog change event after a successful apply or destroy."""

    actions = [TerraformAction.APPLY, TerraformAction.DESTROY]
    config_model = DatadogConfig
    default_priority = {
        TerraformAction.APPLY: 110,
        TerraformAction.DESTROY: 110,
    }

    def __init__(self, config: DatadogConfig) -> None:
        self.config = config
        self._http = urllib3.PoolManager()
        self._ready: bool | None = None  # None = not yet validated

    def is_ready(self) -> bool:
        """Return True only if the API key validates and Datadog is reachable.

        Validated once and cached (in self._ready); a Datadog problem never
        aborts a terraform run -- on any failure we warn and report not-ready so
        the handler is skipped.
        """
        if self._ready is not None:
            return self._ready

        headers = {
            "Accept": "application/json",
            "DD-API-KEY": self.config.resolved_api_key,
        }
        try:
            resp = self._http.request(
                "GET",
                self.config.validate_url,
                headers=headers,
                timeout=urllib3.Timeout(connect=2.0, read=5.0),
            )
        except Exception as e:
            log.warn(f"Datadog credential validation failed, handler disabled: {e}")
            self._ready = False
            return self._ready

        if resp.status != 200:
            body = resp.data.decode("utf-8", errors="replace")
            log.warn(
                f"Datadog credential validation returned {resp.status}, "
                f"handler disabled: {body}"
            )
            self._ready = False
            return self._ready

        # /api/v1/validate returns {"valid": true} on success.
        try:
            valid = json.loads(resp.data.decode("utf-8")).get("valid", False)
        except Exception:
            valid = False
        if not valid:
            log.warn("Datadog reported credentials as invalid, handler disabled")
            self._ready = False
            return self._ready

        log.debug("Datadog credentials validated")
        self._ready = True
        return self._ready

    def execute(
        self,
        action: "TerraformAction",
        stage: "TerraformStage",
        deployment: str,
        definition: "Definition",
        working_dir: str,
        result: Union["TerraformResult", None] = None,
    ) -> None:
        # Only emit on a successful apply/destroy. A "change" only happened if
        # terraform actually completed the action without error.
        if (
            action not in (TerraformAction.APPLY, TerraformAction.DESTROY)
            or stage != TerraformStage.POST
            or result is None
            or result.exit_code != 0
        ):
            return None

        definition_git_info = self._resolve_git_info(
            definition.get_target_path(working_dir)
        )
        payload = self._build_payload(
            action, definition, deployment, result, definition_git_info
        )
        log.debug(f"Sending change event to Datadog: {json.dumps(payload)}")
        self._post_event(payload)
        return None

    def _resolve_git_info(self, working_dir: str) -> dict:
        """Collect git context for the most recent commit.

        All values default to empty strings when unavailable so a
        missing git context never aborts the run.
        """
        info = {
            "branch": "",
            "commit": "",
            "short_commit": "",
            "author_name": "",
            "author_email": "",
            "subject": "",
        }

        info["branch"] = self._git(working_dir, "rev-parse", "--abbrev-ref", "HEAD")

        fields = {
            "commit": "%H",
            "short_commit": "%h",
            "author_name": "%an",
            "author_email": "%ae",
            "subject": "%s",
        }
        for key, fmt in fields.items():
            info[key] = self._git(working_dir, "log", "-1", f"--format={fmt}")

        return info

    @staticmethod
    def _git(working_dir: str, *args: str) -> str:
        """Run a git command, returning stripped stdout or '' on any failure."""
        try:
            return (
                subprocess.check_output(
                    ["git", *args],
                    cwd=working_dir,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                .decode()
                .strip()
            )
        except Exception:
            return ""

    def _build_payload(
        self,
        action: "TerraformAction",
        definition: "Definition",
        deployment: str,
        result: "TerraformResult",
        git_info: dict,
    ) -> dict:
        """Build a v2 Events API change-event payload."""
        author = git_info["author_email"] or git_info["author_name"] or "unknown"

        title = f"Terraform {action.value} of {deployment}/{definition.name}"
        service = definition.name.replace("_", "-")

        category, env = deployment.split("-", 1)

        message_lines = [
            f"Terraform {action.value} completed for `{definition.name}` "
            f"in deployment `{deployment}`.",
        ]

        if any(git_info.values()):
            message_lines.append("Definition Git Info:")
            message_lines.append("---------------------")
        if git_info["subject"]:
            message_lines.append(f"Commit: {git_info['subject']}")
        if git_info["short_commit"]:
            message_lines.append(f"SHA: {git_info['short_commit']}")
        if git_info["branch"]:
            message_lines.append(f"Branch: {git_info['branch']}")
        if git_info["author_name"]:
            author_email = git_info["author_email"]
            if author_email:
                message_lines.append(
                    f"Author: {git_info['author_name']} <{author_email}>"
                )
            else:
                message_lines.append(f"Author: {git_info['author_name']}")
        message = "\n".join(message_lines)

        tags = [
            f"env:{env}",
            f"definition:{definition.name}",
            f"service:{service}",
        ]
        if git_info["branch"]:
            tags.append(f"git_branch:{git_info['branch']}")
        if git_info["short_commit"]:
            tags.append(f"git_commit:{git_info['short_commit']}")

        is_ci = bool(os.environ.get("CI"))
        stdout = strip_ansi(result.stdout_str)
        stderr = strip_ansi(result.stderr_str)

        change_metadata = {
            k: v
            for k, v in {
                "branch": git_info["branch"],
                "commit_sha": git_info["commit"],
                "commit_short_sha": git_info["short_commit"],
                "commit_subject": git_info["subject"],
                "category": category,
                "env": env,
                "action": action.value,
                "definition": definition.model_dump(
                    mode="json", exclude_none=True, exclude_unset=True
                ),
                "is_ci": is_ci,
                "executing_user": os.environ.get("USER"),
                # Capture and truncate to DD's text limit (4096)
                "stdout": (stdout[4000:] + "...") if len(stdout) > 4000 else stdout,
                "stderr": (stderr[4000:] + "...") if len(stderr) > 4000 else stderr,
            }.items()
            if v
        }

        return {
            "data": {
                "type": "event",
                "attributes": {
                    "category": "change",
                    "title": title,
                    "message": message,
                    "tags": tags,
                    "attributes": {
                        "author": {"type": "user", "name": author},
                        "changed_resource": {
                            "type": "configuration",
                            "name": definition.name,
                        },
                        "impacted_resources": [
                            {
                                "type": "service",
                                "name": service,
                            }
                        ],
                        "change_metadata": change_metadata,
                    },
                },
            }
        }

    def _post_event(self, payload: dict) -> None:
        """POST the change event to the Datadog Events API.

        Logs failures as warn and continues
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "DD-API-KEY": self.config.resolved_api_key,
            "DD-APP-KEY": self.config.resolved_app_key,
        }
        try:
            resp = self._http.request(
                "POST",
                self.config.events_url,
                json=payload,
                headers=headers,
                timeout=urllib3.Timeout(connect=2.0, read=5.0),
            )
        except Exception as e:
            log.warn(f"Datadog Event failed to post: {e}")
            return
        if resp.status >= 300:
            body = resp.data.decode("utf-8", errors="replace")
            log.warn(f"Datadog Events API returned {resp.status}: {body}")
            return

        log.debug(f"Posted Datadog change event ({resp.status})")
