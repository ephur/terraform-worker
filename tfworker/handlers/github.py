"""GitHub handler for reporting plan and apply status to a pull request.

Authenticates as a GitHub App and, for plan runs, maintains:

- a rollup check run on the PR head commit whose markdown output carries the
  per-definition job summary, plus one check run per definition (created
  queued, started at plan pre, and concluded from the plan result)
- a live status comment on the pull request (when one is configured),
  updated in place as each definition plans; per-definition detail is
  included only when comment_details is enabled, split across additional
  comments when the body exceeds GitHub's size limit

Applies get their own rollup and per-definition check runs, which keep the
applied plan, and an Apply column in the status comment. An apply-only run
rebuilds the comment from the check runs tagged with its run id, and leaves a
comment reporting another run untouched.

When the openai handler is also configured, its plan summary is embedded in
the per-definition details; otherwise a trimmed copy of the plan output is
used.

Example configuration (all credential fields fall back to environment
variables, so an empty mapping works in GitHub Actions with a configured app)::

    handlers:
      github:
        repository: "myorg/myrepo"          # or GITHUB_REPOSITORY
        pull_request: "{{ env.PR_NUMBER }}" # or GITHUB_PULL_REQUEST / PULL_REQUEST
        app_id: "12345"                     # or GITHUB_APP_ID
        private_key_file: /secrets/app.pem  # or private_key / GITHUB_APP_PRIVATE_KEY(_FILE)
        # check run links; placeholders: run_id, deployment, definition, from_ts, to_ts
        details_url: "https://docs.example.com/deployments/{deployment}"
        logs_url: "https://logs.example.com/search?q=run%3A{run_id}&from={from_ts}&to={to_ts}"
        definition_logs_url: "https://logs.example.com/search?q=run%3A{run_id}%20def%3A{definition}"
        logs_label: "View logs"
"""

import os
import re
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Literal, Mapping
from urllib.parse import quote

import click
from pydantic import BaseModel, Field, field_validator, model_validator

import tfworker.util.log as log
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.exceptions import HandlerError
from tfworker.util.system import strip_ansi

from .base import BaseHandler
from .registry import HandlerRegistry
from .results import BaseHandlerResult

if TYPE_CHECKING:  # pragma: no cover
    from github.CheckRun import CheckRun
    from github.Issue import Issue
    from github.IssueComment import IssueComment
    from github.PullRequest import PullRequest
    from github.Repository import Repository

    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.collection import DefinitionsCollection
    from tfworker.definitions.model import Definition

# GitHub caps comment and check run bodies at 65536 characters; this leaves headroom for markers
BODY_BUDGET = 60000
# room for a detail chunk after a continuation comment's marker, heading and <details> wrapper
DETAIL_CHUNK_LIMIT = BODY_BUDGET - 1000
# consecutive API failures before the handler disables itself
MAX_API_FAILURES = 3
# check run link windows, in seconds: margin around the run, and the open end of a running check
DETAILS_URL_MARGIN = 5 * 60
DETAILS_URL_RUNNING_WINDOW = 6 * 60 * 60


class NoCommitError(HandlerError):
    """No commit SHA could be resolved."""


def _hard_wrap(text: str, width: int) -> str:
    """Hard-wrap long lines, indenting continuations; unbreakable tokens stay intact."""
    if width <= 0:
        return text
    out: list[str] = []
    for line in text.splitlines():
        if len(line) <= width:
            out.append(line)
            continue
        indent = line[: len(line) - len(line.lstrip())] + "    "
        out.extend(
            textwrap.wrap(
                line,
                width=width,
                subsequent_indent=indent,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [line]
        )
    return "\n".join(out)


OUTPUT_CHANGES_HEADING = "Changes to Outputs:"

# where the changes start: resource actions, or outputs for a plan that only changes outputs
PLAN_SECTION_HEADINGS = (
    "Terraform will perform the following actions:",
    OUTPUT_CHANGES_HEADING,
)

# terraform's trailing footer, which follows the changes
PLAN_FOOTER_PREFIXES = (
    "Saved the plan to:",
    "To perform exactly these actions",
    "You can apply this plan to save these new output values",
    "Note: You didn't use the -out option",
)


def _is_plan_footer(line: str) -> bool:
    """Whether a line begins terraform's post-plan footer or its box-drawing rule."""
    if line.startswith(PLAN_FOOTER_PREFIXES):
        return True
    stripped = line.strip()
    return bool(stripped) and set(stripped) == {"─"}


# terraform plan change markers, optionally indented: +, -, ~, -/+, +/-
_PLAN_MARKER = re.compile(r"^( +)([+~-]|[+-]/[+-]) ", re.MULTILINE)


def _diff_format(text: str) -> str:
    """Hoist change markers to column 0 (update/replace become ``!``) for ```diff highlighting."""

    def _sub(m: "re.Match") -> str:
        marker = m.group(2)
        if marker not in ("+", "-"):
            marker = "!"
        return f"{marker}{m.group(1)} "

    return _PLAN_MARKER.sub(_sub, text)


def _fence_safe_truncate(text: str, limit: int) -> str:
    """Truncate markdown on a line boundary, closing any open code fence."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if "\n" in cut:
        cut = cut[: cut.rfind("\n") + 1]
    if cut.count("```") % 2 == 1:
        cut += "```\n"
    return cut + "\n_… truncated_"


def _chunk_markdown(text: str, limit: int) -> list[str]:
    """Split markdown on line boundaries, closing and reopening code fences across chunks."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    open_fence: str | None = None
    for line in text.splitlines(keepends=True):
        if current and size + len(line) > limit:
            if open_fence:
                current.append("```\n")
            chunks.append("".join(current).rstrip("\n"))
            current = [f"{open_fence}\n"] if open_fence else []
            size = sum(len(part) for part in current)
        current.append(line)
        size += len(line)
        if line.strip().startswith("```"):
            # the opening fence line, with any language tag, reopens the next chunk
            open_fence = None if open_fence else line.strip()
    if current:
        chunks.append("".join(current).rstrip("\n"))
    return chunks


def _env_int(label: str, *names: str) -> int | None:
    """The first set environment variable of names as an int; None when unset or non-numeric."""
    raw = next((os.environ[name] for name in names if os.environ.get(name)), None)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warn(f"github handler: ignoring non-numeric {label} {raw!r}")
        return None


# string config fields and the environment variable each falls back to
ENV_FALLBACKS = {
    "repository": "GITHUB_REPOSITORY",
    "app_id": "GITHUB_APP_ID",
    "private_key": "GITHUB_APP_PRIVATE_KEY",
    "private_key_file": "GITHUB_APP_PRIVATE_KEY_FILE",
    "details_url": "GITHUB_CHECK_DETAILS_URL",
    "definition_details_url": "GITHUB_CHECK_DEFINITION_DETAILS_URL",
    "logs_url": "GITHUB_CHECK_LOGS_URL",
    "definition_logs_url": "GITHUB_CHECK_DEFINITION_LOGS_URL",
    "logs_label": "GITHUB_CHECK_LOGS_LABEL",
}

# int config fields: their label in warnings and the environment variables tried in order
ENV_INT_FALLBACKS = {
    "pull_request": ("PR number", ("GITHUB_PULL_REQUEST", "PULL_REQUEST")),
    "installation_id": ("installation id", ("GITHUB_APP_INSTALLATION_ID",)),
}


class GithubConfig(BaseModel):
    repository: str | None = Field(
        default=None,
        description="Repository in 'owner/repo' form; falls back to GITHUB_REPOSITORY.",
    )
    pull_request: int | None = Field(
        default=None,
        description="Pull request number to comment on; falls back to GITHUB_PULL_REQUEST or PULL_REQUEST. When unset, only the check run is maintained.",
    )
    commit_sha: str | None = Field(
        default=None,
        description="Commit sha for the check runs; defaults to the PR head sha, then GITHUB_SHA. Apply-only runs prefer GITHUB_SHA, the planned commit.",
    )
    app_id: str | None = Field(
        default=None,
        description="GitHub App id; falls back to GITHUB_APP_ID.",
    )
    private_key: str | None = Field(
        default=None,
        repr=False,
        description="GitHub App private key (PEM content); falls back to GITHUB_APP_PRIVATE_KEY.",
    )
    private_key_file: str | None = Field(
        default=None,
        description="Path to the GitHub App private key; falls back to GITHUB_APP_PRIVATE_KEY_FILE.",
    )
    installation_id: int | None = Field(
        default=None,
        description="App installation id; discovered from the repository when unset. Falls back to GITHUB_APP_INSTALLATION_ID.",
    )
    check_run_name: str | None = Field(
        default=None,
        description="Name of the check run; defaults to 'tfworker/<deployment>/plan'.",
    )
    apply_check_run_name: str | None = Field(
        default=None,
        description="Name of the apply check run; defaults to 'tfworker/<deployment>/apply'.",
    )
    details_url: str | None = Field(
        default=None,
        description="Template for the rollup check runs' details link; falls back to GITHUB_CHECK_DETAILS_URL. str.format placeholders: {run_id}, {deployment}, {definition} (empty), {from_ts} and {to_ts} (epoch ms).",
    )
    definition_details_url: str | None = Field(
        default=None,
        description="Template for the per-definition check runs' details link, with the placeholders of details_url; falls back to GITHUB_CHECK_DEFINITION_DETAILS_URL, then details_url.",
    )
    logs_url: str | None = Field(
        default=None,
        description="Template for a labelled logs link in the rollup check runs' output, with the placeholders of details_url; falls back to GITHUB_CHECK_LOGS_URL.",
    )
    definition_logs_url: str | None = Field(
        default=None,
        description="Template for the logs link in the per-definition check runs' output; falls back to GITHUB_CHECK_DEFINITION_LOGS_URL, then logs_url.",
    )
    logs_label: str | None = Field(
        default=None,
        description="Text of the logs link; falls back to GITHUB_CHECK_LOGS_LABEL, then 'View logs'.",
    )
    comment_marker: str = "tfworker-status"
    comment_details: bool = Field(
        default=False,
        description="Include per-definition detail (plan output or AI summary) in the PR comment, split across additional comments when needed. When false the comment carries only the status table; detail lives in the check run output.",
    )
    max_detail_chars: int = Field(
        default=8000,
        description="Maximum characters of per-definition detail included in check run output, which cannot be split. PR comments always carry the full detail, split across comments as needed.",
    )
    wrap_width: int = Field(
        default=120,
        description="Hard-wrap fenced plan and error output at this column so check run pages do not scroll horizontally. 0 disables wrapping.",
    )
    required: bool = False

    model_config = {"extra": "forbid"}

    @field_validator(
        *ENV_FALLBACKS,
        *ENV_INT_FALLBACKS,
        "commit_sha",
        "check_run_name",
        "apply_check_run_name",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, v):
        # tolerate empty Jinja renders like pull_request: "{{ env.PR_NUMBER }}"
        if isinstance(v, str) and v.strip().lower() in ("", "null", "none"):
            return None
        return v

    @model_validator(mode="after")
    def _env_fallbacks(self) -> "GithubConfig":
        for name, env in ENV_FALLBACKS.items():
            setattr(self, name, getattr(self, name) or os.environ.get(env) or None)
        self.logs_label = self.logs_label or "View logs"
        for name, (label, envs) in ENV_INT_FALLBACKS.items():
            if getattr(self, name) is None:
                setattr(self, name, _env_int(label, *envs))
        return self

    def settings_errors(self) -> list[str]:
        errors = []
        if not self.repository:
            errors.append("missing repository")
        if not self.app_id:
            errors.append("missing app_id")
        if not self.private_key and not self.private_key_file:
            errors.append("missing private_key or private_key_file")
        if self.private_key and self.private_key_file:
            errors.append("private_key and private_key_file are mutually exclusive")
        return errors


class GithubResult(BaseHandlerResult):
    definition: str
    check_run_url: str | None = None
    definition_check_url: str | None = None
    comment_url: str | None = None


PlanStatus = Literal["pending", "running", "no_changes", "changes", "failed", "skipped"]

STATUS_DISPLAY: dict[PlanStatus, str] = {
    "pending": "⏳ pending",
    "running": "🔄 planning",
    "no_changes": "✅ no changes",
    "changes": "📝 changes",
    "failed": "❌ failed",
    "skipped": "⏭️ skipped",
}

ApplyStatus = Literal["running", "applied", "failed", "not_applied"]

APPLY_DISPLAY: dict[ApplyStatus, str] = {
    "running": "🚀 applying",
    "applied": "✅ applied",
    "failed": "❌ failed",
    "not_applied": "⏭️ not applied",
}

# an apply check run's conclusion read back as its status; unfinished runs have none
APPLY_STATUSES: dict[str, ApplyStatus] = {
    "success": "applied",
    "failure": "failed",
    "skipped": "not_applied",
}

DETAILS_OMITTED = "_Detail sections omitted; body exceeded GitHub's size limit._"


def _link(text: str, url: str) -> str:
    """Markdown text linked to url; the bare text without one."""
    return f"[{text}]({url})" if url else text


def _within_budget(head: list[str], blocks: list[str]) -> str:
    """Check run output of head and detail blocks; the blocks are dropped when over budget."""
    body = "\n".join(head + blocks)
    if len(body) > BODY_BUDGET:
        body = "\n".join(head + [DETAILS_OMITTED])
    return body


@dataclass
class DefinitionStatus:
    """One definition's row in the status report."""

    status: PlanStatus = "pending"
    plan_line: str = ""
    detail: str = ""
    url: str = ""
    apply_status: ApplyStatus | None = None
    apply_line: str = ""
    apply_detail: str = ""
    apply_url: str = ""


class GithubStatusReport:
    """State and markdown rendering for the status comments and check run; makes no API calls."""

    def __init__(
        self,
        deployment: str,
        marker: str,
        max_detail_chars: int,
        include_details: bool = False,
        run_id: str | None = None,
    ) -> None:
        self.deployment = deployment
        self.marker = marker
        self.max_detail_chars = max_detail_chars
        self.include_details = include_details
        self.run_id = run_id
        self.check_url: str | None = None
        self.apply_check_url: str | None = None
        self._rows: dict[str, DefinitionStatus] = {}

    @property
    def rows(self) -> Mapping[str, DefinitionStatus]:
        return self._rows

    @property
    def primary_marker(self) -> str:
        return f"<!-- {self.marker}: {self.deployment} -->"

    def part_marker(self, part: int) -> str:
        return f"<!-- {self.marker}: {self.deployment} part={part} -->"

    def marker_prefix(self) -> str:
        return f"<!-- {self.marker}: {self.deployment}"

    def ensure(self, name: str) -> DefinitionStatus:
        return self._rows.setdefault(name, DefinitionStatus())

    def set_url(self, name: str, url: str) -> None:
        self.ensure(name).url = url

    def mark(
        self, name: str, status: PlanStatus, plan_line: str = "", detail: str = ""
    ) -> None:
        row = self.ensure(name)
        row.status = status
        if plan_line:
            row.plan_line = plan_line
        if detail:
            row.detail = detail

    def set_apply_url(self, name: str, url: str) -> None:
        self.ensure(name).apply_url = url

    def mark_apply(
        self, name: str, status: ApplyStatus, apply_line: str = "", detail: str = ""
    ) -> None:
        row = self.ensure(name)
        row.apply_status = status
        if apply_line:
            row.apply_line = apply_line
        if detail:
            row.apply_detail = detail

    @property
    def has_apply(self) -> bool:
        return any(r.apply_status for r in self._rows.values())

    def finalize(self) -> None:
        """Mark anything that never ran as skipped (aborted or filtered runs)."""
        for row in self._rows.values():
            if row.status in ("pending", "running"):
                row.status = "skipped"

    def finalize_apply(self) -> None:
        """Mark planned changes that were not applied, including any an aborted apply never reached."""
        for row in self._rows.values():
            unapplied = row.status == "changes" and row.apply_status is None
            if unapplied or row.apply_status == "running":
                row.apply_status = "not_applied"

    def conclusion(self) -> str:
        if any(r.status == "failed" for r in self._rows.values()):
            return "failure"
        return "success"

    def apply_conclusion(self, names: Iterable[str]) -> str:
        """The conclusion of the named definitions' applies."""
        if any(self.ensure(n).apply_status == "failed" for n in names):
            return "failure"
        return "success"

    @staticmethod
    def _count(statuses: Iterable[str], display: Mapping[Any, str]) -> str:
        counts: dict[str, int] = {}
        for status in statuses:
            counts[status] = counts.get(status, 0) + 1
        parts = [f"{counts[s]} {display[s]}" for s in display if s in counts]
        return ", ".join(parts) if parts else "no definitions"

    def summary_line(self) -> str:
        return self._count((r.status for r in self._rows.values()), STATUS_DISPLAY)

    def apply_summary_line(self) -> str:
        statuses = (r.apply_status for r in self._rows.values() if r.apply_status)
        return self._count(statuses, APPLY_DISPLAY)

    def _header(
        self,
        kind: str = "plan",
        include_apply: bool = False,
        logs_link: str = "",
        include_plan: bool = True,
    ) -> str:
        lines = [f"## Terraform {kind} status: `{self.deployment}`", ""]
        if self.run_id:
            # the id the stored plans are keyed by, so an apply can request exactly these plans
            lines.append(f"Run `{self.run_id}`")
            lines.append("")
        links = [logs_link] if logs_link else []
        if include_plan and self.check_url:
            links.append(f"[View plan check run]({self.check_url})")
        if include_apply and self.apply_check_url:
            links.append(f"[View apply check run]({self.apply_check_url})")
        if links:
            lines.append(" · ".join(links))
            lines.append("")
        return "\n".join(lines)

    def _table(self, include_apply: bool = False) -> str:
        include_apply = include_apply and self.has_apply
        header = "| Definition | Status | Plan |"
        rule = "| --- | --- | --- |"
        if include_apply:
            header, rule = f"{header} Apply |", f"{rule} --- |"
        lines = [header, rule]
        for name, row in self._rows.items():
            label = _link(f"`{name}`", row.url)
            line = f"| {label} | {STATUS_DISPLAY[row.status]} | {row.plan_line} |"
            if include_apply:
                line += f" {self._apply_cell(row)} |"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _apply_cell(row: DefinitionStatus) -> str:
        if row.apply_status is None:
            return ""
        return _link(APPLY_DISPLAY[row.apply_status], row.apply_url)

    def _apply_table(self) -> str:
        lines = ["| Definition | Apply | Result |", "| --- | --- | --- |"]
        for name, row in self._rows.items():
            if row.apply_status is None:
                continue
            label = _link(f"`{name}`", row.apply_url)
            display = APPLY_DISPLAY[row.apply_status]
            lines.append(f"| {label} | {display} | {row.apply_line} |")
        return "\n".join(lines)

    @staticmethod
    def _wrap_details(
        name: str, display: str, line: str, body: str, part: str = ""
    ) -> str:
        line = f" — {line}" if line else ""
        return (
            "<details>\n"
            f"<summary><code>{name}</code> — {display}{line}{part}</summary>\n\n"
            f"{body}\n\n"
            "</details>"
        )

    def _plan_details(
        self, name: str, row: DefinitionStatus, body: str, part: str = ""
    ) -> str:
        return self._wrap_details(
            name, STATUS_DISPLAY[row.status], row.plan_line, body, part
        )

    def _detail_blocks(self) -> list[str]:
        """Comment detail blocks; a detail too large for one comment is chunked across several."""
        blocks = []
        for name, row in self._rows.items():
            if not row.detail:
                continue
            chunks = _chunk_markdown(row.detail, DETAIL_CHUNK_LIMIT)
            for i, chunk in enumerate(chunks):
                part = f" (part {i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
                blocks.append(self._plan_details(name, row, chunk, part))
        return blocks

    def _check_detail_blocks(self) -> list[str]:
        """Check run detail blocks, each capped at max_detail_chars as check output cannot split."""
        blocks = []
        for name, row in self._rows.items():
            if not row.detail:
                continue
            body = _fence_safe_truncate(row.detail, self.max_detail_chars)
            blocks.append(self._plan_details(name, row, body))
        return blocks

    def render_comment_bodies(self) -> list[str]:
        """The primary comment with the table, plus continuation comments for detail that does not fit."""
        primary = "\n".join(
            [
                self.primary_marker,
                self._header(include_apply=True),
                self._table(include_apply=True),
                "",
            ]
        )
        bodies = [primary]
        if not self.include_details:
            return bodies
        for block in self._detail_blocks():
            if len(bodies[-1]) + len(block) + 2 <= BODY_BUDGET:
                bodies[-1] = f"{bodies[-1]}\n{block}"
            else:
                part = len(bodies) + 1
                bodies.append(
                    "\n".join(
                        [
                            self.part_marker(part),
                            f"_Terraform plan status for `{self.deployment}` "
                            f"(continued, part {part})_",
                            "",
                            block,
                        ]
                    )
                )
        return bodies

    def render_check_summary(self, logs_link: str = "") -> str:
        """The rollup check run output markdown."""
        # the rollup's own page: no link to itself
        header = self._header(logs_link=logs_link, include_plan=False)
        return _within_budget([header, self._table(), ""], self._check_detail_blocks())

    def render_apply_summary(self, logs_link: str = "") -> str:
        """The apply rollup check run output markdown."""
        head = [
            self._header(kind="apply", logs_link=logs_link),
            self._apply_table(),
            "",
        ]
        blocks = []
        for name, row in self._rows.items():
            if row.apply_status is None or not row.apply_detail:
                continue
            body = _fence_safe_truncate(row.apply_detail, self.max_detail_chars)
            display = APPLY_DISPLAY[row.apply_status]
            blocks.append(self._wrap_details(name, display, row.apply_line, body))
        return _within_budget(head, blocks)


@dataclass
class _CheckSet:
    """One action's check runs: the rollup, and per-definition checks that conclude once."""

    action: TerraformAction
    rollup: "CheckRun | None" = None
    checks: dict[str, "CheckRun"] = field(default_factory=dict)
    concluded: set[str] = field(default_factory=set)

    @property
    def kind(self) -> str:
        return str(self.action)


StageFunction = Callable[..., "GithubResult | None"]


@HandlerRegistry.register("github")
class GithubHandler(BaseHandler):
    """Report plan and apply progress and results to GitHub."""

    actions = [TerraformAction.PLAN, TerraformAction.APPLY]
    config_model = GithubConfig
    _ready = False
    default_priority = {
        TerraformAction.PLAN: 90,
        TerraformAction.APPLY: 90,
    }
    # run after openai, when configured, so its plan summary is in the shared results
    dependencies = {
        TerraformAction.PLAN: {TerraformStage.POST: ["openai"]},
    }

    def __init__(
        self, config: GithubConfig, clock: Callable[[], float] = time.time
    ) -> None:
        self.config = config
        self._clock = clock
        self._started_at = clock()
        self._bad_templates: set[str] = set()
        self._app_state = None
        self._repo: "Repository | None" = None
        self._pr: "PullRequest | None" = None
        self._issue: "Issue | None" = None
        self._plan_checks = _CheckSet(TerraformAction.PLAN)
        self._apply_checks = _CheckSet(TerraformAction.APPLY)
        self._deployment = ""
        self._head_sha = ""
        self._planning = True
        self._report: GithubStatusReport | None = None
        self._comments: list["IssueComment"] = []
        self._comments_enabled = True
        self._comment_failures = 0
        self._api_failures = 0
        self.execution_functions: dict[
            TerraformAction, dict[TerraformStage, StageFunction]
        ] = {
            TerraformAction.PLAN: {
                TerraformStage.PRE: self._pre_plan,
                TerraformStage.POST: self._post_plan,
                TerraformStage.ERROR: self._plan_error,
            },
            TerraformAction.APPLY: {
                TerraformStage.PRE: self._pre_apply,
                TerraformStage.POST: self._post_apply,
                TerraformStage.ERROR: self._apply_error,
            },
        }

        errors = config.settings_errors()
        if errors:
            msg = f"github handler misconfigured: {'; '.join(errors)}"
            if config.required:
                raise HandlerError(msg)
            log.warn(f"{msg}; github handler disabled")
            self._ready = False
            return
        self._ready = True

    @property
    def app_state(self):
        if self._app_state is None:
            self._app_state = click.get_current_context().obj
        return self._app_state

    def _run_id(self) -> str | None:
        """The run id plans are stored under; None when the run has none."""
        try:
            return self.app_state.root_options.run_id or None
        except (AttributeError, RuntimeError):  # no click context or root options
            return None

    @property
    def report(self) -> GithubStatusReport:
        if self._report is None:
            raise HandlerError("github handler: no report before setup")
        return self._report

    @property
    def repo(self) -> "Repository":
        if self._repo is None:
            raise HandlerError("github handler: not connected")
        return self._repo

    def is_ready(self) -> bool:
        return self._ready

    ###########################################################################
    # lifecycle
    ###########################################################################
    def setup(
        self,
        deployment: str,
        definitions: "DefinitionsCollection",
        working_dir: str,
        terraform_options,
    ) -> None:
        """Authenticate, create the plan check runs, and seed the comment; an apply-only run rebuilds the report instead."""
        if not self._ready:
            return
        if not terraform_options.plan and not terraform_options.apply:
            log.debug("github handler: neither plan nor apply requested")
            self._ready = False
            return
        self._planning = bool(terraform_options.plan)
        self._deployment = deployment
        self._started_at = self._clock()
        try:
            self._connect()
            run_id = self._run_id()
            self._report = GithubStatusReport(
                deployment=deployment,
                marker=self.config.comment_marker,
                max_detail_chars=self.config.max_detail_chars,
                include_details=self.config.comment_details,
                run_id=run_id,
            )
            self._head_sha = self._resolve_sha(for_apply=not self._planning)
            if not self._planning:
                self._load_run_state(run_id)
                return

            for defn in definitions.values():
                self.report.ensure(defn.name)
            # needs the report: the claimed marker is scoped to the deployment
            self._claim_comments_safely()

            self._create_rollup(self._plan_checks)
            for defn in definitions.values():
                check = self._create_definition_check(
                    self._plan_checks, defn.name, "queued"
                )
                self.report.set_url(defn.name, check.html_url)
            self._update_comments()
        except Exception as e:
            self._ready = False
            if isinstance(e, NoCommitError) and not self.config.required:
                self._report = None
                log.info(f"github handler: {e.message}; skipping")
                return
            log.error(f"github handler setup failed: {e}")
            if self.config.required:
                raise HandlerError(f"github handler setup failed: {e}") from e

    def execute(
        self,
        action: "TerraformAction",
        stage: "TerraformStage",
        deployment: str,
        definition: "Definition",
        working_dir: str,
        result: "TerraformResult | None" = None,
    ) -> GithubResult | None:
        if not self._ready or self._report is None:
            return None
        function = self.execution_functions.get(action, {}).get(stage)
        if function is None or (stage != TerraformStage.PRE and result is None):
            return None
        try:
            ret = function(definition, result)
        except Exception as e:
            self._record_api_failure(f"{action} {stage} for {definition.name}", e)
            return None
        self._api_failures = 0
        return ret

    def teardown(self, deployment: str, working_dir: str) -> None:
        """Conclude the check runs and finalize the comment."""
        if self._report is None:
            return
        applied = self._apply_checks.rollup is not None
        # each kind is concluded on its own, so one failing does not stop the other
        if self._planning:
            self._conclude_checks(self._plan_checks)
        if applied:
            self._conclude_checks(self._apply_checks)
        # an apply-only run that applied nothing leaves the comment as it was
        if self._planning or applied:
            self._update_comments()

    def _conclude_checks(self, checks: _CheckSet) -> None:
        """Finalize the report, skip unconcluded definition checks and complete the rollup."""
        try:
            report = self.report
            if checks.kind == "plan":
                report.finalize()
                conclusion, counts = report.conclusion(), report.summary_line()
                skipped = "Plan skipped"
            else:
                report.finalize_apply()
                conclusion = report.apply_conclusion(checks.checks)
                counts, skipped = report.apply_summary_line(), "Apply not completed"
            for name in checks.checks:
                self._conclude_check(checks, name, "skipped", skipped)
            if checks.rollup is not None:
                checks.rollup.edit(
                    status="completed",
                    conclusion=conclusion,
                    output={
                        "title": f"Terraform {checks.kind} {conclusion}: {counts}",
                        "summary": self._rollup_summary(checks, concluded=True),
                    },
                    **self._details_url(concluded=True),
                )
        except Exception as e:
            log.error(f"github handler {checks.kind} teardown failed: {e}")

    ###########################################################################
    # plan handling
    ###########################################################################
    def _pre_plan(
        self, definition: "Definition", result: "TerraformResult | None" = None
    ) -> None:
        self.report.mark(definition.name, "running")
        check = self._plan_checks.checks.get(definition.name)
        if check is not None:
            check.edit(status="in_progress")
        self._update_comments()

    def _post_plan(
        self, definition: "Definition", result: "TerraformResult"
    ) -> GithubResult:
        name = definition.name
        if result.exit_code == 0:
            self.report.mark(name, "no_changes", plan_line="No changes.")
            self._conclude_check(self._plan_checks, name, "success", "No changes.")
        elif result.has_changes():
            text = strip_ansi(result.stdout_str)
            detail = self._summary_for(definition) or self._trimmed_plan(
                text, self.config.wrap_width
            )
            plan_line = self._plan_line(text)
            self.report.mark(name, "changes", plan_line=plan_line, detail=detail)
            self._conclude_check(
                self._plan_checks,
                name,
                "success",
                plan_line or "Changes planned",
                detail,
            )
        else:
            self._plan_failed(definition, result)
        return self._progress(self._plan_checks, definition, TerraformStage.POST)

    def _plan_error(self, definition: "Definition", result: "TerraformResult") -> None:
        # no result: the error stage of a plan has never added one to the shared results
        self._plan_failed(definition, result)
        self._progress(self._plan_checks, definition, TerraformStage.ERROR)

    def _plan_failed(self, definition: "Definition", result: "TerraformResult") -> None:
        detail = self._error_detail(result)
        self.report.mark(definition.name, "failed", detail=detail)
        self._conclude_check(
            self._plan_checks,
            definition.name,
            "failure",
            "Terraform plan failed",
            detail,
        )

    def _summary_for(self, definition: "Definition") -> str | None:
        """The openai-generated summary for this definition, if any."""
        if definition.plan_file is None:
            return None
        try:
            expected = str(
                Path(definition.plan_file)
                .with_suffix(".tfplan.json")
                .with_suffix(".summary.md")
            )
            for r in self.app_state.handlers.get_results(handler_name="openai"):
                if getattr(r, "task", None) == "summary" and r.file == expected:
                    return r.content
        except Exception as e:
            log.debug(f"github handler: unable to read openai results: {e}")
        return None

    @staticmethod
    def _plan_line(text: str) -> str:
        # a plan that only changes outputs has no "Plan:" line; name that case instead
        outputs_only = False
        for line in text.splitlines():
            if line.startswith("Plan:"):
                return line.strip()
            if line.startswith(OUTPUT_CHANGES_HEADING):
                outputs_only = True
        return "Output changes only." if outputs_only else ""

    @staticmethod
    def _trimmed_plan(text: str, wrap_width: int = 0) -> str:
        """The planned changes, from the first section heading to terraform's footer, fenced for markdown."""
        capture = False
        lines = []
        for line in text.splitlines():
            if not capture and line.startswith(PLAN_SECTION_HEADINGS):
                capture = True
            elif capture and _is_plan_footer(line):
                capture = False
            if capture:
                lines.append(line)
        trimmed = "\n".join(lines).rstrip()
        if not trimmed:
            return ""
        return f"```diff\n{_hard_wrap(_diff_format(trimmed), wrap_width)}\n```"

    def _error_detail(self, result: "TerraformResult") -> str:
        text = strip_ansi(result.stderr_str or result.stdout_str)
        lines = [line for line in text.splitlines() if line.strip()]
        snippet = _hard_wrap("\n".join(lines[-20:]), self.config.wrap_width)
        return f"```\n{snippet}\n```" if snippet else ""

    ###########################################################################
    # apply handling
    ###########################################################################
    def _pre_apply(
        self, definition: "Definition", result: "TerraformResult | None" = None
    ) -> None:
        name = definition.name
        row = self.report.ensure(name)
        # a definition with a stored plan planned changes, even when its plan check was not read back
        if not self._planning and row.status == "pending":
            row.status = "changes"
        self.report.mark_apply(name, "running")
        if self._apply_checks.rollup is None:
            self._create_rollup(self._apply_checks)
        check = self._create_definition_check(self._apply_checks, name, "in_progress")
        self.report.set_apply_url(name, check.html_url)
        self._update_comments()
        self._update_rollup(self._apply_checks)

    def _post_apply(
        self, definition: "Definition", result: "TerraformResult"
    ) -> GithubResult:
        text = strip_ansi(result.stdout_str)
        apply_line = self._apply_line(text)
        detail = self._apply_output(text, self.config.wrap_width)
        self.report.mark_apply(
            definition.name, "applied", apply_line=apply_line, detail=detail
        )
        self._conclude_check(
            self._apply_checks,
            definition.name,
            "success",
            apply_line or "Apply complete",
            detail,
        )
        return self._progress(self._apply_checks, definition, TerraformStage.POST)

    def _apply_error(
        self, definition: "Definition", result: "TerraformResult"
    ) -> GithubResult:
        detail = self._error_detail(result)
        self.report.mark_apply(definition.name, "failed", detail=detail)
        self._conclude_check(
            self._apply_checks,
            definition.name,
            "failure",
            "Terraform apply failed",
            detail,
        )
        return self._progress(self._apply_checks, definition, TerraformStage.ERROR)

    @staticmethod
    def _apply_line(text: str) -> str:
        for line in text.splitlines():
            if line.startswith("Apply complete!"):
                return line.strip()
        return ""

    @staticmethod
    def _apply_output(text: str, wrap_width: int = 0) -> str:
        """The apply log with blank-line runs collapsed, fenced for markdown."""
        lines: list[str] = []
        for line in text.rstrip().splitlines():
            if line.strip() or (lines and lines[-1].strip()):
                lines.append(line.rstrip())
        trimmed = "\n".join(lines).strip("\n")
        if not trimmed:
            return ""
        return f"```\n{_hard_wrap(trimmed, wrap_width)}\n```"

    ###########################################################################
    # github api plumbing
    ###########################################################################
    def _connect(self) -> None:
        from github import Auth, GithubIntegration

        pem = self.config.private_key or Path(self.config.private_key_file).read_text()
        integration = GithubIntegration(auth=Auth.AppAuth(self.config.app_id, pem))
        owner, repo_name = self.config.repository.split("/", 1)
        installation_id = (
            self.config.installation_id
            or integration.get_repo_installation(owner, repo_name).id
        )
        gh = integration.get_github_for_installation(installation_id)
        self._repo = gh.get_repo(self.config.repository)
        if self.config.pull_request:
            self._pr = self.repo.get_pull(self.config.pull_request)
            self._issue = self.repo.get_issue(self.config.pull_request)

    def _check_name(self, kind: str) -> str:
        """The rollup check run name of a kind; definition checks append ': <definition>'."""
        configured = (
            self.config.check_run_name
            if kind == "plan"
            else self.config.apply_check_run_name
        )
        return configured or f"tfworker/{self._deployment}/{kind}"

    def _create_check_run(
        self, name: str, status: str, definition: str | None = None, **kwargs
    ) -> "CheckRun":
        """Create a check run on the reported commit, tagged with the run id and its details link."""
        return self.repo.create_check_run(
            name=name,
            head_sha=self._head_sha,
            status=status,
            **kwargs,
            **self._external_id(),
            **self._details_url(definition),
        )

    def _create_rollup(self, checks: _CheckSet) -> None:
        checks.rollup = self._create_check_run(
            self._check_name(checks.kind),
            "in_progress",
            output={
                "title": f"Terraform {checks.kind} in progress",
                "summary": self._rollup_summary(checks),
            },
        )
        if checks.kind == "plan":
            self.report.check_url = checks.rollup.html_url
        else:
            self.report.apply_check_url = checks.rollup.html_url

    def _create_definition_check(
        self, checks: _CheckSet, name: str, status: str
    ) -> "CheckRun":
        check = self._create_check_run(
            f"{self._check_name(checks.kind)}: {name}", status, definition=name
        )
        checks.checks[name] = check
        return check

    def _rollup_summary(self, checks: _CheckSet, concluded: bool = False) -> str:
        logs_link = self._logs_link(concluded=concluded)
        if checks.kind == "plan":
            return self.report.render_check_summary(logs_link)
        return self.report.render_apply_summary(logs_link)

    def _update_rollup(self, checks: _CheckSet) -> None:
        if checks.rollup is None:
            return
        checks.rollup.edit(
            output={
                "title": f"Terraform {checks.kind} in progress",
                "summary": self._rollup_summary(checks),
            }
        )

    def _progress(
        self, checks: _CheckSet, definition: "Definition", stage: TerraformStage
    ) -> GithubResult:
        """Refresh the comment and rollup after a definition concludes; the stage's result."""
        self._update_comments()
        self._update_rollup(checks)
        def_check = checks.checks.get(definition.name)
        return GithubResult(
            handler="github",
            action=checks.action,
            stage=stage,
            definition=definition.name,
            check_run_url=checks.rollup.html_url if checks.rollup else None,
            definition_check_url=def_check.html_url if def_check else None,
            comment_url=self._comments[0].html_url if self._comments else None,
        )

    def _conclude_check(
        self,
        checks: _CheckSet,
        name: str,
        conclusion: str,
        title: str,
        summary: str = "",
    ) -> None:
        """Complete a per-definition check run once; an apply's text keeps the applied plan."""
        check = checks.checks.get(name)
        if check is None or name in checks.concluded:
            return
        checks.concluded.add(name)
        output = {
            "title": title[:255],
            "summary": _fence_safe_truncate(
                self._with_links(summary, name, checks.kind), BODY_BUDGET
            ),
        }
        row = self.report.rows.get(name)
        if checks.kind == "apply" and row is not None and row.detail:
            output["text"] = _fence_safe_truncate(
                f"### Applied plan\n\n{row.detail}", BODY_BUDGET
            )
        check.edit(
            status="completed",
            conclusion=conclusion,
            output=output,
            **self._details_url(name, concluded=True),
        )

    def _external_id(self) -> dict[str, Any]:
        """Check run kwargs tagging it with the run id."""
        run_id = self._run_id()
        return {"external_id": run_id} if run_id else {}

    def _details_url(
        self, definition: str | None = None, concluded: bool = False
    ) -> dict[str, Any]:
        """Check run kwargs setting its details link."""
        url = self._check_url("details", definition, concluded)
        return {"details_url": url} if url else {}

    def _logs_link(self, definition: str | None = None, concluded: bool = False) -> str:
        """The labelled markdown logs link for a check run's output; empty without a template."""
        url = self._check_url("logs", definition, concluded)
        return f"[{self.config.logs_label}]({url})" if url else ""

    def _rollup_label(self, kind: str) -> str:
        return f"View {self._deployment} {kind} check run"

    def _with_links(self, summary: str, definition: str, kind: str) -> str:
        """A concluded per-definition check's summary, headed by its logs and rollup links."""
        report = self.report
        rollup_url = report.check_url if kind == "plan" else report.apply_check_url
        links = [self._logs_link(definition, concluded=True)]
        if rollup_url:
            links.append(f"[{self._rollup_label(kind)}]({rollup_url})")
        line = " · ".join(link for link in links if link)
        if not line:
            return summary
        return f"{line}\n\n{summary}" if summary else line

    def _without_links(self, summary: str | None) -> str:
        """A per-definition check summary read back without the links line heading it."""
        summary = summary or ""
        first, _, rest = summary.partition("\n\n")
        labels = [self.config.logs_label, *map(self._rollup_label, ("plan", "apply"))]
        links = first.split(" · ")
        if "\n" not in first and all(
            link.endswith(")") and any(link.startswith(f"[{lb}](") for lb in labels)
            for link in links
        ):
            return rest
        return summary

    def _check_url(
        self, kind: str, definition: str | None, concluded: bool
    ) -> str | None:
        """Render the configured {kind}_url template; a concluded check's window closes at conclusion."""
        template = getattr(self.config, f"{kind}_url")
        if definition is not None:
            template = getattr(self.config, f"definition_{kind}_url") or template
        if not template or template in self._bad_templates:
            return None
        window = DETAILS_URL_MARGIN if concluded else DETAILS_URL_RUNNING_WINDOW
        values = {
            "run_id": quote(self._run_id() or "", safe=""),
            "deployment": quote(self._deployment, safe=""),
            "definition": quote(definition or "", safe=""),
            "from_ts": int((self._started_at - DETAILS_URL_MARGIN) * 1000),
            "to_ts": int((self._clock() + window) * 1000),
        }
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError, AttributeError, TypeError) as e:
            self._bad_templates.add(template)
            log.warn(
                f"github handler: ignoring {kind} url template {template!r}: {e!r}"
            )
            return None

    def _resolve_sha(self, for_apply: bool = False) -> str:
        """The commit to report on: commit_sha, then the PR head or GITHUB_SHA, whichever the action prefers."""
        # GITHUB_SHA is a merge commit on pull_request plan events; an apply's names the planned commit
        pr_head = self._pr.head.sha if self._pr is not None else None
        env_sha = os.environ.get("GITHUB_SHA")
        fallbacks = (env_sha, pr_head) if for_apply else (pr_head, env_sha)
        sha = self.config.commit_sha or next((s for s in fallbacks if s), None)
        if not sha:
            raise NoCommitError("no commit to report on; set commit_sha or GITHUB_SHA")
        return sha

    def _claim_comments(self) -> None:
        """Adopt this deployment's status comments from a prior run so they are edited, not duplicated."""
        if self._issue is None or self._report is None:
            return
        primary_marker = self.report.primary_marker
        part_prefix = f"{self.report.marker_prefix()} part="
        primary, parts = None, []
        for comment in self._issue.get_comments():
            # exact first-line match: one deployment must not claim another whose name it prefixes
            first_line = (comment.body or "").split("\n", 1)[0].strip()
            if first_line == primary_marker:
                primary = comment
            elif first_line.startswith(part_prefix):
                parts.append(comment)
        self._comments = ([primary] if primary else []) + parts

    def _claim_comments_safely(self) -> None:
        try:
            self._claim_comments()
        except Exception as e:
            # without the existing comments an update would post duplicates
            self._disable_comments(f"unable to read existing comments: {e}")

    def _disable_comments(self, reason: str) -> None:
        """Stop updating the status comment; check runs are unaffected."""
        if self._comments_enabled and self._issue is not None:
            log.warn(f"github handler: not updating the status comment; {reason}")
        self._comments_enabled = False

    def _load_run_state(self, run_id: str | None) -> None:
        """Rebuild the report from the run's check runs and claim its comment; failures only disable the comment."""
        if not run_id:
            self._disable_comments("no run id to match the plan to")
            return
        try:
            found = self._load_check_runs(run_id)
        except Exception as e:
            self._disable_comments(f"unable to read the plan check runs: {e}")
            return
        if not found:
            self._disable_comments(
                f"no plan check run for run {run_id} on {self._head_sha[:12]}"
            )
            return
        self._claim_comments_safely()
        if self._comments and f"Run `{run_id}`" not in (self._comments[0].body or ""):
            self._disable_comments("it reports a different run")

    def _load_check_runs(self, run_id: str) -> bool:
        """Populate the report from the run's plan and apply check runs; False without a plan rollup."""
        runs = sorted(
            self.repo.get_commit(self._head_sha).get_check_runs(filter="all"),
            key=lambda r: r.id,
        )
        rollup = next(
            (
                r
                for r in reversed(runs)
                if r.name == self._check_name("plan") and r.external_id == run_id
            ),
            None,
        )
        if rollup is None:
            return False
        self.report.check_url = rollup.html_url
        plan_prefix = f"{self._check_name('plan')}: "
        apply_prefix = f"{self._check_name('apply')}: "
        plans: dict[str, "CheckRun"] = {}
        applies: dict[str, "CheckRun"] = {}
        # ascending ids: the newest check wins while keeping the definition's first position
        for run in runs:
            if run.name.startswith(plan_prefix):
                # untagged per-definition checks predate external_id; match those created after the rollup
                if run.external_id == run_id or (
                    not run.external_id and run.id > rollup.id
                ):
                    plans[run.name.removeprefix(plan_prefix)] = run
            elif run.name.startswith(apply_prefix) and run.external_id == run_id:
                applies[run.name.removeprefix(apply_prefix)] = run
        for name, run in plans.items():
            status, plan_line = self._plan_status(run)
            summary = self._without_links(run.output.summary if run.output else "")
            self.report.mark(name, status, plan_line=plan_line, detail=summary)
            self.report.set_url(name, run.html_url)
        for name, run in applies.items():
            apply_status = APPLY_STATUSES.get(run.conclusion)
            if apply_status is None:
                continue
            title = (run.output.title if run.output else "") or ""
            summary = self._without_links(run.output.summary if run.output else "")
            self.report.mark_apply(
                name,
                apply_status,
                apply_line=title.strip() if apply_status == "applied" else "",
                detail=summary,
            )
            self.report.set_apply_url(name, run.html_url)
        return True

    @staticmethod
    def _plan_status(run: "CheckRun") -> tuple[PlanStatus, str]:
        """A plan check run's status and plan line; the title separates changes from none."""
        if run.conclusion == "failure":
            return "failed", ""
        if run.conclusion == "skipped":
            return "skipped", ""
        if run.conclusion != "success":
            return "pending", ""
        title = ((run.output.title if run.output else "") or "").strip()
        if title.lower().startswith("no changes"):
            return "no_changes", title
        return "changes", "" if title == "Changes planned" else title

    def _update_comments(self) -> None:
        if self._issue is None or self._report is None or not self._comments_enabled:
            return
        try:
            bodies = self.report.render_comment_bodies()
            for i, body in enumerate(bodies):
                if i < len(self._comments):
                    if self._comments[i].body != body:
                        self._comments[i].edit(body)
                else:
                    self._comments.append(self._issue.create_comment(body))
            # remove continuation comments left over from a larger update
            while len(self._comments) > len(bodies):
                self._comments.pop().delete()
        except Exception as e:
            self._comment_failures += 1
            log.warn(f"github handler: status comment update failed: {e}")
            if self._comment_failures >= MAX_API_FAILURES:
                self._disable_comments(f"{self._comment_failures} consecutive failures")
            return
        self._comment_failures = 0

    def _record_api_failure(self, context: str, exc: Exception) -> None:
        self._api_failures += 1
        log.error(f"github handler {context} failed: {exc}")
        if self._api_failures >= MAX_API_FAILURES:
            log.warn(
                f"github handler disabled after {self._api_failures} consecutive API failures"
            )
            self._ready = False
