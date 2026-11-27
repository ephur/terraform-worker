"""Slack handler for terraform-worker.

Posts a compact, live-updating Block Kit report for each run: one channel
message (a ``container`` header card plus a ``plan`` "Run progress" activity
feed) and one threaded reply holding the full per-definition ``data_table``.
The channel message stays a fixed size regardless of how many definitions the
run contains; the thread table holds the full detail.

Example configuration::

    handlers:
      slack:
        channel: "#terraform-runs"
        token: "xoxb-..."          # raw value; supports jinja injection
        # token_env: "SLACK_BOT_TOKEN"  # env var name (default)
        title: "apps/qa"           # optional; falls back to deployment name
        links:                     # optional links shown in the header card
          - text: "Argo workflow"
            url: "https://argo.example/workflows/ops/{{ env.WORKFLOW_NAME }}"
          - text: "logs"
            url: "https://app.datadoghq.com/logs?query=service%3Aterraform-worker"
        # per-definition deep link used on failure cards; {definition} is
        # replaced with the definition name
        definition_log_url_template: "https://app.datadoghq.com/logs?query=service%3Aterraform-worker%20%40definition%3A{definition}"
        update_interval: 4.0       # min seconds between Slack updates
        timezone: "America/Chicago"  # for timestamp fallback text; live
                                     # timestamps render in the viewer's tz

Notes on Slack API behavior this module encodes (all confirmed live):

- ``has_header_divider`` and ``is_collapsible`` on a container are mutually
  exclusive at post/update time.
- ``data_table`` cells reject ``raw_number``; numeric columns use ``raw_text``.
- ``plan`` blocks keep a stable ``block_id`` across updates: a fresh id makes
  Slack treat the block as new and reset its expanded/collapsed state.
- The ``<!date>`` command renders in section/context blocks (including
  container children) but NOT in the container title or subtitle, where the
  raw syntax is displayed literally.
- ``blocks.validate`` accepts payloads the message APIs reject; only real
  ``chat.postMessage``/``chat.update`` calls prove a payload.
- The bot token only has ``chat:write``/``chat:write.public`` so every posted
  message ``ts`` is tracked locally (the bot cannot read the thread back).
"""

import copy
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Union
from zoneinfo import ZoneInfo

import click
from pydantic import BaseModel, Field, PrivateAttr, model_validator
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import tfworker.util.log as log
from tfworker.custom_types.terraform import TerraformAction, TerraformStage

from .base import BaseHandler
from .registry import HandlerRegistry

if TYPE_CHECKING:
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition

TERMINAL_STATUSES = frozenset({"done", "changes", "failed", "skipped"})

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PLAN_LINE_RE = re.compile(r"Plan: [^\n]*")
_PLAN_COUNT_RE = re.compile(r"(\d+) to (?:import|add|change|destroy)")
_APPLY_LINE_RE = re.compile(r"(?:Apply|Destroy) complete! Resources: [^\n]*")
_APPLY_COUNT_RE = re.compile(r"(\d+) (?:imported|added|changed|destroyed)")


class SlackLink(BaseModel):
    text: str
    url: str


class SlackConfig(BaseModel):
    channel: str
    token: str | None = Field(default=None, repr=False)
    token_env: str = "SLACK_BOT_TOKEN"
    title: str | None = None
    links: list[SlackLink] = Field(default_factory=list)
    definition_log_url_template: str | None = None
    update_interval: float = Field(default=4.0, ge=0.0)
    timezone: str = "America/Chicago"

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


def _rich_text(elements: list[dict]) -> dict:
    return {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": elements}],
    }


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


class DefinitionRecord:
    """Per-definition state accumulated over a run."""

    __slots__ = (
        "name",
        "expected",
        "statuses",
        "started_at",
        "action_started",
        "work_secs",
        "plan_line",
        "planned_changes",
        "applied_changes",
        "error_action",
        "error_snippet",
    )

    def __init__(self, name: str) -> None:
        self.name = name
        # actions this definition is expected to run; seeded from the run's
        # expected actions, extended per definition (e.g. always_apply)
        self.expected: list[str] = []
        self.statuses: dict[str, str] = {}
        self.started_at: float | None = None
        self.action_started: dict[str, float] = {}
        self.work_secs: float = 0.0
        self.plan_line: str | None = None
        self.planned_changes: int | None = None
        self.applied_changes: int | None = None
        self.error_action: str | None = None
        self.error_snippet: str | None = None

    def duration_secs(self) -> int | None:
        """Seconds spent actually running actions for this definition.

        Summed per action rather than start-to-finish: all inits run (in
        parallel) long before a definition's serial plan/apply, so wall time
        would report near-whole-run durations for every definition.
        """
        return int(self.work_secs) if self.work_secs else None


class SlackStatusBoard:
    """Builds and maintains the run's Slack messages.

    One channel message (container + plan block) is posted at setup and
    updated in place; the per-definition table is posted as threaded
    ``data_table`` replies. Updates are debounced to ``update_interval``
    seconds (terminal flushes bypass the debounce) and 429s are honored.
    """

    # data_table emoji per status; skipped renders as plain "—" text
    RICH_STATUS_EMOJI: dict[str, str] = {
        "pending": "hourglass",
        "running": "arrows_counterclockwise",
        "done": "white_check_mark",
        "changes": "large_blue_circle",
        "failed": "x",
    }
    ACTION_ORDER: list[str] = ["init", "plan", "apply", "destroy"]
    ACTION_NOUNS: dict[str, tuple[str, str, str]] = {
        # action -> (noun, present participle, outcome phrase)
        # plan's outcome reads "N with changes", complementing "N no changes"
        "plan": ("Plan", "Planning", "with changes"),
        "apply": ("Apply", "Applying", "applied"),
        "destroy": ("Destroy", "Destroying", "destroyed"),
        "init": ("Init", "Initializing", "initialized"),
    }
    MAX_ERROR_CARDS: int = 5
    MAX_NAMED_RUNNING: int = 10
    MAX_FAILURE_TABLE_ROWS: int = 5
    MAX_DATA_TABLE_ROWS: int = 200
    MAX_DATA_TABLE_CHARS: int = 18000
    DATA_TABLE_PAGE_SIZE: int = 15
    FAILURE_ERROR_CHARS: int = 80
    CARD_ERROR_CHARS: int = 200

    def __init__(
        self,
        config: SlackConfig,
        run_id: str | None,
        backend_plans: bool = False,
    ) -> None:
        self._config = config
        self._channel = config.channel
        self._run_id = run_id
        self._backend_plans = backend_plans

        self._records: dict[str, DefinitionRecord] = {}
        self._expected_actions: list[str] = []
        self._primary_action: str = "plan"
        self._deployment: str | None = None
        self._branch: str | None = None
        self._commit: str | None = None

        self._started_monotonic = time.monotonic()
        self._started_wall = datetime.now(timezone.utc)

        self._lock = threading.RLock()
        self._sending = False
        self._next_update_at = 0.0
        self._ts: str | None = None
        self._thread_ts: list[str] = []
        self._main_sent: list | None = None
        self._thread_sent: list[list] = []

    # ------------------------------------------------------------------
    # state updates
    # ------------------------------------------------------------------
    def ensure_definition(
        self, definition_name: str, deployment: str, working_dir: str
    ) -> None:
        """Register a definition and capture run context on first call."""
        with self._lock:
            if self._deployment is None:
                self._deployment = deployment
                self._resolve_git_context(working_dir)
            if definition_name not in self._records:
                rec = DefinitionRecord(definition_name)
                rec.expected = list(self._expected_actions)
                self._records[definition_name] = rec

    def add_definition_action(
        self, definition_name: str, action: TerraformAction
    ) -> None:
        """Expect an extra action for one definition (e.g. always_apply).

        Kept per definition rather than run-wide: one always_apply definition
        must not make a plan-only run present as an apply, nor give every
        other definition an apply expectation it can never satisfy.
        """
        with self._lock:
            rec = self._records[definition_name]
            if action.value not in rec.expected:
                rec.expected = [
                    a for a in self.ACTION_ORDER if a in rec.expected + [action.value]
                ]

    def set_expected_actions(self, actions: list[TerraformAction]) -> None:
        """Set the action columns for the run and derive the primary action."""
        with self._lock:
            vals = [a.value for a in actions]
            self._expected_actions = [a for a in self.ACTION_ORDER if a in vals]
            for candidate in ("destroy", "apply", "plan", "init"):
                if candidate in self._expected_actions:
                    self._primary_action = candidate
                    break

    def mark(self, definition_name: str, action: TerraformAction, status: str) -> None:
        """Update the status of a definition+action pair."""
        with self._lock:
            action_val = action.value
            if definition_name not in self._records:
                rec = DefinitionRecord(definition_name)
                rec.expected = list(self._expected_actions)
                self._records[definition_name] = rec
            rec = self._records[definition_name]
            if action_val not in rec.expected:
                rec.expected = [
                    a for a in self.ACTION_ORDER if a in rec.expected + [action_val]
                ]
            rec.statuses[action_val] = status
            now = time.monotonic()
            if status == "running":
                if rec.started_at is None:
                    rec.started_at = now
                rec.action_started[action_val] = now
            elif status in TERMINAL_STATUSES and action_val in rec.action_started:
                rec.work_secs += now - rec.action_started.pop(action_val)

    def record_result(
        self,
        definition_name: str,
        action: TerraformAction,
        result: Union["TerraformResult", None],
        failed: bool = False,
    ) -> None:
        """Capture plan/apply output details or an error snippet for a definition."""
        if result is None:
            return
        with self._lock:
            rec = self._records.get(definition_name)
            if rec is None:
                return
            # errors="replace": providers can emit non-UTF-8 bytes, and a decode
            # error here must not take down the run
            text = _ANSI_RE.sub(
                "",
                result.stdout.decode(errors="replace")
                + "\n"
                + result.stderr.decode(errors="replace"),
            )
            if failed:
                rec.error_action = action.value
                rec.error_snippet = self._extract_error(text)
                return
            if action == TerraformAction.PLAN:
                match = _PLAN_LINE_RE.search(text)
                if match:
                    rec.plan_line = match.group(0).rstrip(".")
                    rec.planned_changes = sum(
                        int(n) for n in _PLAN_COUNT_RE.findall(match.group(0))
                    )
                elif "No changes." in text:
                    rec.planned_changes = 0
            elif action in (TerraformAction.APPLY, TerraformAction.DESTROY):
                match = _APPLY_LINE_RE.search(text)
                if match:
                    rec.applied_changes = sum(
                        int(n) for n in _APPLY_COUNT_RE.findall(match.group(0))
                    )

    def expects(self, action: TerraformAction, definition_name: str) -> bool:
        """Return True when the given definition is expected to run the action."""
        rec = self._records.get(definition_name)
        return rec is not None and action.value in rec.expected

    def finalize(self) -> None:
        """Resolve statuses left pending/running as skipped (run over or aborted)."""
        with self._lock:
            for rec in self._records.values():
                for action_val in rec.expected:
                    if rec.statuses.get(action_val, "pending") in (
                        "pending",
                        "running",
                    ):
                        rec.statuses[action_val] = "skipped"

    @staticmethod
    def _extract_error(text: str) -> str:
        """Pull the most useful error line(s) out of terraform output."""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for i, line in enumerate(lines):
            if "Error:" in line:
                return " ".join(lines[i : i + 3])
        return " ".join(lines[-3:]) if lines else "unknown error"

    def _resolve_git_context(self, working_dir: str) -> None:
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

        self._branch = branch or None
        self._commit = commit or None

    # ------------------------------------------------------------------
    # classification / aggregation
    # ------------------------------------------------------------------
    def _classify(self, rec: DefinitionRecord) -> str:
        """Classify a definition as failed, running, ok, no_changes, skipped, or queued.

        "no_changes" means the plan ran and found nothing to do; "skipped" is
        reserved for definitions whose work never ran at all (e.g. an aborted
        run resolved by teardown) — conflating them would misreport clean
        plans as not having run.
        """
        vals = [rec.statuses.get(a, "pending") for a in rec.expected]
        if "failed" in vals:
            return "failed"
        if "running" in vals:
            return "running"
        if not (vals and all(v in TERMINAL_STATUSES for v in vals)):
            return "queued"
        if all(v == "skipped" for v in vals):
            return "skipped"
        if "plan" in rec.expected and rec.statuses.get("plan") == "skipped":
            return "skipped"
        if not rec.applied_changes and (
            rec.planned_changes == 0 or rec.statuses.get("apply") == "skipped"
        ):
            return "no_changes"
        return "ok"

    @staticmethod
    def _running_action(rec: DefinitionRecord) -> str | None:
        for action_val in rec.expected:
            if rec.statuses.get(action_val) == "running":
                return action_val
        return None

    def _running_verb(self, recs: list[DefinitionRecord]) -> str:
        """Verb for what these definitions are actually doing right now.

        The run's primary verb would be wrong here: during the parallel init
        phase a plan run would label everything "planning".
        """
        actions = {self._running_action(r) for r in recs} - {None}
        if len(actions) == 1:
            return self.ACTION_NOUNS[actions.pop()][1].lower()
        return "running"

    def _buckets(self) -> dict[str, list[DefinitionRecord]]:
        buckets: dict[str, list[DefinitionRecord]] = {
            "failed": [],
            "running": [],
            "ok": [],
            "no_changes": [],
            "skipped": [],
            "queued": [],
        }
        for rec in self._records.values():
            buckets[self._classify(rec)].append(rec)
        buckets["running"].sort(key=lambda r: r.started_at or 0.0)
        return buckets

    def is_terminal(self) -> bool:
        """Return True when no definition remains queued or running."""
        buckets = self._buckets()
        return not buckets["running"] and not buckets["queued"]

    def overall_status(self) -> str:
        """Return 'in_progress', 'failed', or 'done'."""
        if not self.is_terminal():
            return "in_progress"
        if self._buckets()["failed"]:
            return "failed"
        return "done"

    def _total_resource_changes(self) -> int:
        applied = sum(r.applied_changes or 0 for r in self._records.values())
        if applied:
            return applied
        return sum(r.planned_changes or 0 for r in self._records.values())

    def _nouns(self) -> tuple[str, str, str]:
        return self.ACTION_NOUNS[self._primary_action]

    def _elapsed_text(self) -> str:
        secs = int(time.monotonic() - self._started_monotonic)
        if secs < 90:
            return f"{secs}s"
        return f"{round(secs / 60)} min"

    def _clock(self, dt: datetime) -> str:
        """Fallback clock text in the configured timezone (UTC if unknown)."""
        try:
            dt = dt.astimezone(ZoneInfo(self._config.timezone))
        except Exception:
            pass
        return dt.strftime("%H:%M %Z")

    def _run_key(self) -> str:
        raw = self._run_id or self._deployment or "run"
        return "tfworker_" + re.sub(r"[^A-Za-z0-9_]", "_", str(raw))

    def _display_name(self) -> str:
        return self._config.title or self._deployment or "terraform"

    # ------------------------------------------------------------------
    # block building
    # ------------------------------------------------------------------
    def _links_suffix(self) -> str:
        return "".join(f" · <{lk.url}|{lk.text}>" for lk in self._config.links)

    def _subtitle(self) -> str:
        parts: list[str] = []
        if self._run_id:
            parts.append(f"run `{self._run_id}`")
        if self._branch:
            branch = f"branch `{self._branch}`"
            if self._commit:
                branch += f" (`{self._commit}`)"
            parts.append(branch)
        elif self._commit:
            parts.append(f"commit `{self._commit}`")
        if self.overall_status() == "in_progress":
            # plain clock: the container subtitle does not process Slack's
            # <!date> command — it renders the syntax literally. The live
            # "updated N ago" lives in the links context, which does.
            parts.append(f"started {self._clock(self._started_wall)}")
        else:
            parts.append(f"finished in {self._elapsed_text()}")
        return " · ".join(parts)

    def _status_section(self, buckets: dict) -> dict:
        _, ing, past = self._nouns()
        total = len(self._records)
        overall = self.overall_status()
        if overall == "done":
            # report real outcomes: teardown bulk-skips definitions an aborted
            # run never reached, so "N definitions succeeded" would mislead
            parts = [f"{len(buckets['ok'])} {past}"]
            if buckets["no_changes"]:
                parts.append(f"{len(buckets['no_changes'])} no changes")
            if buckets["skipped"]:
                parts.append(f"{len(buckets['skipped'])} skipped")
            text = f":white_check_mark: *Run complete — {', '.join(parts)}*"
        elif overall == "failed":
            failed = len(buckets["failed"])
            text = f":x: *Run failed — {failed} of {total} definitions errored*"
        else:
            finished = (
                len(buckets["failed"])
                + len(buckets["ok"])
                + len(buckets["no_changes"])
                + len(buckets["skipped"])
            )
            running = len(buckets["running"])
            if running:
                phase = self._running_verb(buckets["running"]).capitalize()
                text = (
                    f":arrows_counterclockwise: *{phase}* — {running} "
                    f"definition{'s' if running != 1 else ''} running · "
                    f"{finished} of {total} finished"
                )
            else:
                text = (
                    f":arrows_counterclockwise: *{ing}* — "
                    f"{finished} of {total} finished"
                )
        return {
            "type": "section",
            "block_id": "run_status",
            "text": {"type": "mrkdwn", "text": text},
        }

    def _counts_context(self, buckets: dict) -> dict:
        _, _, past = self._nouns()
        overall = self.overall_status()

        elements: list[dict] = [
            {"type": "mrkdwn", "text": f"*{len(self._records)}* definitions"}
        ]
        if buckets["ok"]:
            elements.append(
                {
                    "type": "mrkdwn",
                    "text": f":white_check_mark: *{len(buckets['ok'])}* {past}",
                }
            )
        if buckets["failed"]:
            elements.append(
                {"type": "mrkdwn", "text": f":x: *{len(buckets['failed'])}* failed"}
            )
        if overall == "in_progress":
            if buckets["running"]:
                elements.append(
                    {
                        "type": "mrkdwn",
                        "text": f":arrows_counterclockwise: *{len(buckets['running'])}* running",
                    }
                )
            if buckets["queued"]:
                elements.append(
                    {
                        "type": "mrkdwn",
                        "text": f":hourglass: *{len(buckets['queued'])}* queued",
                    }
                )
        if buckets["no_changes"]:
            elements.append(
                {
                    "type": "mrkdwn",
                    "text": f":heavy_minus_sign: *{len(buckets['no_changes'])}* no changes",
                }
            )
        if buckets["skipped"]:
            elements.append(
                {
                    "type": "mrkdwn",
                    "text": f":fast_forward: *{len(buckets['skipped'])}* skipped",
                }
            )
        if overall != "in_progress":
            changes = self._total_resource_changes()
            if changes:
                label = (
                    "resource changes planned"
                    if self._primary_action == "plan"
                    else "resources changed"
                )
                elements.append({"type": "mrkdwn", "text": f"{changes} {label}"})
        return {"type": "context", "block_id": "run_counts", "elements": elements}

    def _links_context(self) -> dict:
        text = ":thread: per-definition table in thread" + self._links_suffix()
        elements = [{"type": "mrkdwn", "text": text}]
        if self.overall_status() == "in_progress":
            now = datetime.now(timezone.utc)
            elements.append(
                {
                    "type": "mrkdwn",
                    "text": (
                        f"updated <!date^{int(now.timestamp())}^{{ago}}"
                        f"|{self._clock(now)}>"
                    ),
                }
            )
        return {
            "type": "context",
            "block_id": "run_links",
            "elements": elements,
        }

    def _failures_table(self, failed: list[DefinitionRecord]) -> list[dict]:
        rows: list[list[dict]] = [
            [
                {"type": "raw_text", "text": "Definition"},
                {"type": "raw_text", "text": "Stage"},
                {"type": "raw_text", "text": "Error"},
            ]
        ]
        for rec in failed[: self.MAX_FAILURE_TABLE_ROWS]:
            rows.append(
                [
                    _rich_text(
                        [{"type": "text", "text": rec.name, "style": {"code": True}}]
                    ),
                    {"type": "raw_text", "text": rec.error_action or "?"},
                    {
                        "type": "raw_text",
                        "text": _truncate(
                            rec.error_snippet or "unknown error",
                            self.FAILURE_ERROR_CHARS,
                        ),
                    },
                ]
            )
        remainder = len(failed) - self.MAX_FAILURE_TABLE_ROWS
        more_text = ":thread: full sortable table in thread" + self._links_suffix()
        if remainder > 0:
            more_text = f"…and *{remainder} more* failures — " + more_text
        return [
            {"type": "divider", "block_id": "verdict_divider"},
            {
                "type": "table",
                "block_id": "failures_table",
                "column_settings": [
                    {"is_wrapped": False},
                    {"align": "center"},
                    {"is_wrapped": True},
                ],
                "rows": rows,
            },
            {
                "type": "context",
                "block_id": "failures_more",
                "elements": [{"type": "mrkdwn", "text": more_text}],
            },
        ]

    def _build_container(self, buckets: dict) -> dict:
        overall = self.overall_status()
        noun, _, _ = self._nouns()
        children: list[dict] = [
            self._status_section(buckets),
            self._counts_context(buckets),
        ]
        if overall == "failed":
            children.extend(self._failures_table(buckets["failed"]))
        else:
            children.append(self._links_context())

        block: dict = {
            "type": "container",
            "block_id": f"{self._run_key()}_container",
            "title": {
                "type": "plain_text",
                "text": f"{noun} — {self._display_name()}",
            },
            "subtitle": {"type": "mrkdwn", "text": self._subtitle()},
            "child_blocks": children,
        }
        # has_header_divider and is_collapsible are mutually exclusive on the
        # Slack API; final states collapse, running states get the divider.
        if overall == "in_progress":
            block["has_header_divider"] = True
        else:
            block["is_collapsible"] = True
        return block

    def _error_task(self, rec: DefinitionRecord) -> dict:
        task: dict = {
            "task_id": f"fail_{rec.name}",
            "title": f"{rec.name} — {rec.error_action or 'run'} failed",
            "status": "error",
            "output": _rich_text(
                [
                    {
                        "type": "text",
                        "text": _truncate(
                            rec.error_snippet or "unknown error",
                            self.CARD_ERROR_CHARS,
                        ),
                        "style": {"code": True},
                    }
                ]
            ),
        }
        template = self._config.definition_log_url_template
        if template:
            task["sources"] = [
                {
                    "type": "url",
                    # not str.format(): URLs legitimately contain braces, which
                    # would raise and take down the run
                    "url": template.replace("{definition}", rec.name),
                    "text": "full error in Datadog",
                }
            ]
        return task

    @staticmethod
    def _name_list(recs: list[DefinitionRecord], cap: int = 10) -> str:
        names = ", ".join(r.name for r in recs[:cap])
        if len(recs) > cap:
            names += ", …"
        return names

    def _rollup_complete_task(self, buckets: dict) -> dict | None:
        _, _, outcome = self._nouns()
        ok = buckets["ok"]
        if not ok:
            return None
        changes = self._total_resource_changes()
        label = (
            "resource changes planned"
            if self._primary_action == "plan"
            else "resources changed"
        )
        summary = f"{changes} {label}" if changes else "no resource changes"
        return {
            "task_id": "rollup_complete",
            "title": f"{len(ok)} definitions {outcome}",
            "status": "complete",
            "details": _rich_text([{"type": "text", "text": self._name_list(ok)}]),
            "output": _rich_text(
                [
                    {
                        "type": "text",
                        "text": f"{summary} · {self._elapsed_text()} elapsed",
                    }
                ]
            ),
        }

    def _build_plan_block(self, buckets: dict) -> dict | None:
        """The "Run Details" feed: failures, then outcome rollups (with
        changes / no changes / skipped), then per-definition running tasks
        and the queue while the run is live."""
        overall = self.overall_status()
        if overall == "failed":
            # final failures live in the container's table instead
            return None

        tasks: list[dict] = []

        for rec in buckets["failed"][: self.MAX_ERROR_CARDS]:
            tasks.append(self._error_task(rec))

        rollup = self._rollup_complete_task(buckets)
        if rollup:
            tasks.append(rollup)
        # shown while running too: clean finishes are completed work, and on
        # an all-clean run this is otherwise the feed's only sign of progress
        if buckets["no_changes"]:
            tasks.append(
                {
                    "task_id": "rollup_no_changes",
                    "title": (
                        f"{len(buckets['no_changes'])} definitions with no changes"
                    ),
                    "status": "complete",
                    "details": _rich_text(
                        [
                            {
                                "type": "text",
                                "text": self._name_list(buckets["no_changes"]),
                            }
                        ]
                    ),
                }
            )
        if buckets["skipped"]:
            tasks.append(
                {
                    "task_id": "rollup_skipped",
                    "title": f"{len(buckets['skipped'])} definitions skipped — not run",
                    "status": "complete",
                    "details": _rich_text(
                        [
                            {
                                "type": "text",
                                "text": self._name_list(buckets["skipped"]),
                            }
                        ]
                    ),
                }
            )

        if overall == "done":
            stored = len([r for r in self._records.values() if r.planned_changes])
            if self._backend_plans and self._primary_action == "plan" and stored:
                run_ref = f" keyed by run {self._run_id}" if self._run_id else ""
                tasks.append(
                    {
                        "task_id": "rollup_plans",
                        "title": "Plans stored in S3",
                        "status": "complete",
                        "output": _rich_text(
                            [
                                {
                                    "type": "text",
                                    "text": f"{stored} plans{run_ref} — "
                                    "re-applyable via --backend-plans",
                                }
                            ]
                        ),
                    }
                )
        else:
            # every running definition gets its own task showing the step it
            # is on; the rollup only absorbs overflow past MAX_NAMED_RUNNING
            running = buckets["running"]
            for rec in running[: self.MAX_NAMED_RUNNING]:
                task: dict = {
                    "task_id": f"run_{rec.name}",
                    "title": f"{rec.name} — {self._running_verb([rec])}",
                    "status": "in_progress",
                }
                if rec.plan_line:
                    task["details"] = _rich_text(
                        [{"type": "text", "text": rec.plan_line}]
                    )
                tasks.append(task)
            extra_running = running[self.MAX_NAMED_RUNNING :]
            if extra_running:
                tasks.append(
                    {
                        "task_id": "rollup_running",
                        "title": (
                            f"…and {len(extra_running)} more "
                            f"{self._running_verb(extra_running)}"
                        ),
                        "status": "in_progress",
                        "details": _rich_text(
                            [{"type": "text", "text": self._name_list(extra_running)}]
                        ),
                    }
                )
            if buckets["queued"]:
                queued = buckets["queued"]
                tasks.append(
                    {
                        "task_id": "rollup_queued",
                        "title": f"{len(queued)} definitions queued",
                        "status": "pending",
                        "details": _rich_text(
                            [
                                {
                                    "type": "text",
                                    "text": f"next up: {self._name_list(queued, cap=3)}",
                                }
                            ]
                        ),
                    }
                )

        if not tasks:
            return None
        return {
            "type": "plan",
            # stable block_id: a fresh one per update makes Slack treat the
            # block as new and reset its expanded/collapsed state, collapsing
            # the feed under a watching user
            "block_id": f"{self._run_key()}_plan",
            "title": "📝 Run Details",
            "tasks": tasks,
        }

    def _build_main_blocks(self) -> list[dict]:
        buckets = self._buckets()
        blocks: list[dict] = [self._build_container(buckets)]
        plan_block = self._build_plan_block(buckets)
        if plan_block:
            blocks.append(plan_block)
        return blocks

    @staticmethod
    def _cell_chars(cell: dict) -> int:
        if cell["type"] == "raw_text":
            return len(cell["text"])
        return sum(
            len(el.get("text") or el.get("name") or "")
            for el in cell["elements"][0]["elements"]
        )

    def _status_cell(self, status: str) -> dict:
        if status == "skipped":
            return {"type": "raw_text", "text": "—"}
        return _rich_text(
            [{"type": "emoji", "name": self.RICH_STATUS_EMOJI.get(status, "question")}]
        )

    def _build_thread_messages(self) -> list[list[dict]]:
        """Per-definition data_table messages, chunked under the row limit."""
        if not (self._records and self._expected_actions):
            return []

        noun, _, _ = self._nouns()
        # column per action any definition expects (always_apply can add an
        # apply column to a plan-only run for just those definitions)
        columns = [
            a
            for a in self.ACTION_ORDER
            if any(a in r.expected for r in self._records.values())
        ]
        header = [{"type": "raw_text", "text": "Definition"}]
        header += [{"type": "raw_text", "text": a.capitalize()} for a in columns]
        header += [
            {"type": "raw_text", "text": "Changed"},
            {"type": "raw_text", "text": "Ran (s)"},
        ]

        data_rows: list[list[dict]] = []
        for rec in self._records.values():
            row = [
                _rich_text(
                    [{"type": "text", "text": rec.name, "style": {"code": True}}]
                )
            ]
            for action_val in columns:
                if action_val not in rec.expected:
                    row.append({"type": "raw_text", "text": "—"})
                else:
                    row.append(
                        self._status_cell(rec.statuses.get(action_val, "pending"))
                    )
            changed = (
                rec.applied_changes
                if rec.applied_changes is not None
                else rec.planned_changes
            )
            # Slack rejects empty raw_text cells ("must be more than 0
            # characters"); unknown values render as an em dash
            row.append(
                {"type": "raw_text", "text": "—" if changed is None else str(changed)}
            )
            duration = rec.duration_secs()
            row.append(
                {
                    "type": "raw_text",
                    "text": "—" if duration is None else f"{duration}s",
                }
            )
            data_rows.append(row)

        caption = f"Per-definition results — {noun.lower()} {self._display_name()}" + (
            f" run {self._run_id}" if self._run_id else ""
        )
        # chunk on both the 200-row limit and Slack's 20k-char aggregate cell
        # limit per message (long definition names can hit chars before rows)
        chunks: list[list[list[dict]]] = [[]]
        chars = 0
        for row in data_rows:
            row_chars = sum(self._cell_chars(cell) for cell in row)
            if chunks[-1] and (
                len(chunks[-1]) >= self.MAX_DATA_TABLE_ROWS
                or chars + row_chars > self.MAX_DATA_TABLE_CHARS
            ):
                chunks.append([])
                chars = 0
            chunks[-1].append(row)
            chars += row_chars

        messages: list[list[dict]] = []
        n_chunks = len(chunks)
        for i, chunk in enumerate(chunks):
            blocks: list[dict] = []
            if i == 0:
                heading = (
                    f":clipboard: *Per-definition results* — "
                    f"{noun.lower()} `{self._display_name()}`"
                )
                if self._run_id:
                    heading += f", run `{self._run_id}`"
                heading += " (sortable & filterable)"
                blocks.append(
                    {
                        "type": "section",
                        "block_id": "detail_heading",
                        "text": {"type": "mrkdwn", "text": heading},
                    }
                )
            blocks.append(
                {
                    "type": "data_table",
                    "block_id": f"{self._run_key()}_table_{i}",
                    "caption": caption
                    + (f" (part {i + 1}/{n_chunks})" if n_chunks > 1 else ""),
                    "page_size": self.DATA_TABLE_PAGE_SIZE,
                    "row_header_column_index": 0,
                    "rows": [header] + chunk,
                }
            )
            messages.append(blocks)
        return messages

    def _fallback_text(self) -> str:
        noun, _, past = self._nouns()
        buckets = self._buckets()
        total = len(self._records)
        overall = self.overall_status()
        name = self._display_name()
        if overall == "done":
            parts = [f"{len(buckets['ok'])} {past}"]
            if buckets["no_changes"]:
                parts.append(f"{len(buckets['no_changes'])} no changes")
            if buckets["skipped"]:
                parts.append(f"{len(buckets['skipped'])} skipped")
            return f"{noun} {name}: complete — {', '.join(parts)}"
        if overall == "failed":
            failed = len(buckets["failed"])
            return f"{noun} {name}: failed — {failed} of {total} definitions errored"
        finished = (
            len(buckets["failed"])
            + len(buckets["ok"])
            + len(buckets["no_changes"])
            + len(buckets["skipped"])
        )
        text = f"{noun} {name}: in progress — {finished} of {total} finished"
        if buckets["failed"]:
            text += f", {len(buckets['failed'])} failed"
        return text

    # ------------------------------------------------------------------
    # posting
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_for_compare(blocks: list[dict]) -> list[dict]:
        """Blocks minus volatile fields, so no-op updates can be skipped.

        The subtitle's "started" clock and the links context's live
        "updated N ago" timestamp change with every render; neither alone
        should force a send.
        """
        normalized = copy.deepcopy(blocks)
        for block in normalized:
            if block.get("type") != "container":
                continue
            block["subtitle"] = {}
            for child in block.get("child_blocks", []):
                if child.get("block_id") == "run_links":
                    child["elements"] = [
                        e
                        for e in child["elements"]
                        if not e.get("text", "").startswith("updated ")
                    ]
        return normalized

    def _call_api(self, method, force: bool, **kwargs):
        """Invoke a Slack API method, honoring Retry-After on 429.

        Non-forced calls drop the update on a 429 (the next flush resends the
        latest snapshot); forced (terminal) calls sleep and retry.
        """
        attempts = 5 if force else 1
        for attempt in range(attempts):
            try:
                return method(**kwargs)
            except SlackApiError as e:
                status = getattr(e.response, "status_code", None)
                if status != 429:
                    raise
                retry_after = int(e.response.headers.get("Retry-After", "5"))
                if attempt + 1 < attempts:
                    time.sleep(min(retry_after, 30))
                    continue
                self._next_update_at = time.monotonic() + max(
                    retry_after, self._config.update_interval
                )
                if force:
                    # a forced flush is usually the run's last; there is no
                    # later call to pick up the deferral
                    log.error("Slack rate limited; final status update was dropped")
                else:
                    log.warn(f"Slack rate limited; deferring update {retry_after}s")
                return None
        return None

    def post_or_update(self, client: WebClient, force: bool = False) -> None:
        """Post/refresh the channel message and the threaded detail table.

        Updates are debounced to ``update_interval`` seconds so bursts of
        definition completions coalesce into one snapshot; ``force`` bypasses
        the debounce for initial/terminal flushes. Messages whose content has
        not changed are not re-sent.
        """
        with self._lock:
            now = time.monotonic()
            # skip when debounced or another thread is mid-flush; its snapshot
            # is stale but the next flush resends the latest state
            if not force and (now < self._next_update_at or self._sending):
                return
            self._sending = True
            fallback = self._fallback_text()
            final = self.overall_status() != "in_progress"
            main_blocks = self._build_main_blocks()
            thread_messages = self._build_thread_messages()

        # Slack calls happen outside the state lock so worker threads marking
        # progress during parallel init are never blocked on network I/O;
        # _sending keeps senders (and the message-tracking fields) exclusive.
        try:
            # 1) Channel message — the thread root.
            try:
                if self._ts is None:
                    resp = self._call_api(
                        client.chat_postMessage,
                        force,
                        channel=self._channel,
                        blocks=main_blocks,
                        text=fallback,
                    )
                    if resp is not None:
                        self._ts = resp["ts"]
                        self._channel = resp["channel"]
                        self._main_sent = self._normalize_for_compare(main_blocks)
                elif self._normalize_for_compare(main_blocks) != self._main_sent or (
                    final
                ):
                    resp = self._call_api(
                        client.chat_update,
                        force,
                        channel=self._channel,
                        ts=self._ts,
                        blocks=main_blocks,
                        text=fallback,
                    )
                    if resp is not None:
                        self._main_sent = self._normalize_for_compare(main_blocks)
            except Exception as e:
                log.error(f"Slack API error updating run message: {e}")

            # 2) Threaded per-definition table (needs the thread root).
            for i, blocks in enumerate(thread_messages if self._ts else []):
                try:
                    if i < len(self._thread_ts):
                        if (
                            i < len(self._thread_sent)
                            and blocks == self._thread_sent[i]
                        ):
                            continue
                        resp = self._call_api(
                            client.chat_update,
                            force,
                            channel=self._channel,
                            ts=self._thread_ts[i],
                            blocks=blocks,
                            text=f"Per-definition results: {self._display_name()}",
                        )
                        if resp is not None:
                            while len(self._thread_sent) <= i:
                                self._thread_sent.append(None)
                            self._thread_sent[i] = blocks
                    else:
                        resp = self._call_api(
                            client.chat_postMessage,
                            force,
                            channel=self._channel,
                            thread_ts=self._ts,
                            blocks=blocks,
                            text=f"Per-definition results: {self._display_name()}",
                        )
                        if resp is not None:
                            self._thread_ts.append(resp["ts"])
                            self._thread_sent.append(blocks)
                except Exception as e:
                    log.error(f"Slack API error updating detail message {i + 1}: {e}")
        finally:
            with self._lock:
                self._sending = False
                # max() preserves a longer deferral set by a 429 Retry-After;
                # advancing even on failure keeps the debounce engaged so a
                # broken channel/token cannot turn every event into a retry
                self._next_update_at = max(
                    self._next_update_at,
                    time.monotonic() + self._config.update_interval,
                )


@HandlerRegistry.register("slack")
class SlackHandler(BaseHandler):
    """Post a live-updating Slack report for each terraform-worker run."""

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
        self._board = SlackStatusBoard(
            config=config,
            run_id=self._get_root_option("run_id"),
            backend_plans=bool(self._get_root_option("backend_plans")),
        )
        self._ready = True

    def is_ready(self) -> bool:
        return self._ready

    @staticmethod
    def _get_root_option(attr: str):
        """Read an option from app state; return None on any failure."""
        try:
            return getattr(click.get_current_context().obj.root_options, attr)
        except Exception:
            return None

    def setup(
        self,
        deployment: str,
        definitions,
        working_dir: str,
        terraform_options,
    ) -> None:
        """Pre-populate the board with all definitions and expected actions."""
        try:
            expected_actions = [TerraformAction.INIT]
            if getattr(terraform_options, "plan", False) or getattr(
                terraform_options, "plan_destroy", False
            ):
                expected_actions.append(TerraformAction.PLAN)
            if getattr(terraform_options, "apply", False):
                expected_actions.append(TerraformAction.APPLY)
            if getattr(terraform_options, "destroy", False):
                expected_actions.append(TerraformAction.DESTROY)

            self._board.set_expected_actions(expected_actions)
            for defn in definitions.values():
                self._board.ensure_definition(defn.name, deployment, working_dir)
                # always_apply applies right after its plan even in plan-only
                # runs — an expectation for this definition, not the whole run
                if getattr(defn, "always_apply", False):
                    self._board.add_definition_action(defn.name, TerraformAction.APPLY)

            self._board.post_or_update(self._client, force=True)
        except Exception as e:
            log.error(f"SlackHandler.setup error: {e}")

    def teardown(
        self,
        deployment: str,
        working_dir: str,
    ) -> None:
        """Finalize the board; resolve any unfinished statuses."""
        try:
            self._board.finalize()
            self._board.post_or_update(self._client, force=True)
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
        # never let status reporting take down the run: exec_handlers callers
        # only catch HandlerError, so anything raised here would propagate
        try:
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
                self._board.record_result(
                    definition.name, action, result, failed=(status == "failed")
                )
                # a clean plan with no changes means apply will be skipped; mark
                # it now so finished counts stay accurate during the run
                if (
                    action == TerraformAction.PLAN
                    and status == "done"
                    and self._board.expects(TerraformAction.APPLY, definition.name)
                ):
                    self._board.mark(definition.name, TerraformAction.APPLY, "skipped")
                self._board.post_or_update(self._client)

            elif stage == TerraformStage.ERROR:
                self._board.mark(definition.name, action, "failed")
                self._board.record_result(definition.name, action, result, failed=True)
                self._board.post_or_update(self._client)
        except Exception as e:
            log.error(f"SlackHandler.execute error: {e}")
