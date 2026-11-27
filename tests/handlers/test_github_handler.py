from types import SimpleNamespace
from unittest import mock

import pytest

from tfworker.commands.terraform import TerraformResult
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.exceptions import HandlerError
from tfworker.handlers.github import (
    GithubConfig,
    GithubHandler,
    GithubStatusReport,
    NoCommitError,
    _chunk_markdown,
    _diff_format,
    _fence_safe_truncate,
    _hard_wrap,
)

PLAN = TerraformAction.PLAN
APPLY = TerraformAction.APPLY
PRE = TerraformStage.PRE
POST = TerraformStage.POST
ERROR = TerraformStage.ERROR

GITHUB_ENV_VARS = [
    "GITHUB_REPOSITORY",
    "GITHUB_PULL_REQUEST",
    "PULL_REQUEST",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_PRIVATE_KEY_FILE",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_SHA",
    "GITHUB_CHECK_DETAILS_URL",
    "GITHUB_CHECK_DEFINITION_DETAILS_URL",
    "GITHUB_CHECK_LOGS_URL",
    "GITHUB_CHECK_DEFINITION_LOGS_URL",
    "GITHUB_CHECK_LOGS_LABEL",
]


@pytest.fixture(autouse=True)
def clean_github_env(monkeypatch):
    """Tests may run inside GitHub Actions; pin the environment."""
    for var in GITHUB_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_config(**overrides) -> GithubConfig:
    settings = {
        "repository": "myorg/myrepo",
        "app_id": "12345",
        "private_key": "---PEM---",
        "pull_request": 7,
    }
    settings.update(overrides)
    return GithubConfig.model_validate(settings)


def make_report(**overrides) -> GithubStatusReport:
    settings = {
        "deployment": "dep",
        "marker": "tfworker-status",
        "max_detail_chars": 100,
    }
    settings.update(overrides)
    return GithubStatusReport(**settings)


def make_handler(config=None, with_pr=True, clock=None) -> GithubHandler:
    """A handler as setup() leaves it, with the GitHub objects mocked."""
    handler = GithubHandler(config or make_config(), clock=clock or FakeClock())
    handler._app_state = mock.Mock()
    handler._app_state.handlers.get_results.return_value = []
    handler._app_state.root_options.run_id = None
    handler._repo = mock.Mock()
    if with_pr:
        handler._pr = mock.Mock()
        handler._issue = mock.Mock()
        handler._issue.get_comments.return_value = []
        handler._issue.create_comment.return_value.html_url = (
            "https://github.test/comment/1"
        )
    handler._plan_checks.rollup = mock.Mock(html_url="https://github.test/check/1")
    handler._plan_checks.checks = {
        "mydef": mock.Mock(html_url="https://github.test/check/mydef")
    }
    handler._deployment = "dep"
    handler._head_sha = "abc123"
    handler._report = make_report(max_detail_chars=8000)
    handler._report.ensure("mydef")
    return handler


def make_definition(name="mydef", plan_file="/tmp/plans/mydef.tfplan"):
    definition = mock.Mock()
    definition.name = name
    definition.plan_file = plan_file
    return definition


def execute(
    handler,
    action,
    stage,
    result=None,
    name="mydef",
    plan_file="/tmp/plans/mydef.tfplan",
):
    return handler.execute(
        action=action,
        stage=stage,
        deployment="dep",
        definition=make_definition(name, plan_file),
        working_dir="/tmp",
        result=result,
    )


def run_setup(handler, plan=True, apply=False, definitions=None):
    """Run setup() with the connection and commit lookup mocked."""
    handler._connect = mock.Mock()
    handler._resolve_sha = mock.Mock(return_value="abc123")
    if definitions is None:
        definitions = {"mydef": make_definition()}
    handler.setup("dep", definitions, "/tmp", SimpleNamespace(plan=plan, apply=apply))


def apply_checks(handler):
    """Queue the apply rollup and definition check runs the next apply creates."""
    rollup = mock.Mock(html_url="https://github.test/apply")
    per_def = mock.Mock(html_url="https://github.test/apply/mydef")
    handler._repo.create_check_run.side_effect = [rollup, per_def]
    return rollup, per_def


def check_run(id, name, conclusion="success", title="", summary="", external_id=None):
    run = mock.Mock()
    run.id = id
    run.name = name
    run.conclusion = conclusion
    run.external_id = external_id
    run.html_url = f"https://github.test/runs/{id}"
    run.output = SimpleNamespace(title=title, summary=summary)
    return run


PLAN_STDOUT = (
    b"Terraform used the selected providers\n"
    b"Terraform will perform the following actions:\n"
    b"  # null_resource.example will be created\n"
    b"Plan: 1 to add, 0 to change, 0 to destroy.\n"
)

APPLY_STDOUT = (
    b"null_resource.example: Creating...\n"
    b"null_resource.example: Creation complete after 0s [id=123]\n"
    b"\n"
    b"\n"
    b"Apply complete! Resources: 1 added, 0 changed, 0 destroyed.\n"
)

NO_CHANGES = TerraformResult(0, b"No changes.", b"")


class TestGithubConfig:
    def test_env_fallbacks(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "envorg/envrepo")
        monkeypatch.setenv("GITHUB_APP_ID", "999")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "envpem")
        monkeypatch.setenv("GITHUB_PULL_REQUEST", "42")
        monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "777")
        monkeypatch.setenv("GITHUB_CHECK_DETAILS_URL", "https://logs.example.com/a")
        monkeypatch.setenv(
            "GITHUB_CHECK_DEFINITION_DETAILS_URL", "https://logs.example.com/b"
        )
        monkeypatch.setenv("GITHUB_CHECK_LOGS_URL", "https://logs.example.com/c")
        monkeypatch.setenv(
            "GITHUB_CHECK_DEFINITION_LOGS_URL", "https://logs.example.com/d"
        )
        monkeypatch.setenv("GITHUB_CHECK_LOGS_LABEL", "Logs")

        config = GithubConfig()

        assert config.repository == "envorg/envrepo"
        assert config.app_id == "999"
        assert config.private_key == "envpem"
        assert config.pull_request == 42
        assert config.installation_id == 777
        assert config.details_url == "https://logs.example.com/a"
        assert config.definition_details_url == "https://logs.example.com/b"
        assert config.logs_url == "https://logs.example.com/c"
        assert config.definition_logs_url == "https://logs.example.com/d"
        assert config.logs_label == "Logs"
        assert config.settings_errors() == []

    def test_pull_request_env_fallback_alternate_name(self, monkeypatch):
        monkeypatch.setenv("PULL_REQUEST", "13")
        config = GithubConfig()
        assert config.pull_request == 13

    def test_explicit_config_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "envorg/envrepo")
        monkeypatch.setenv("GITHUB_CHECK_DETAILS_URL", "https://logs.example.com/a")
        monkeypatch.setenv("GITHUB_CHECK_LOGS_URL", "https://logs.example.com/a")
        monkeypatch.setenv("GITHUB_CHECK_LOGS_LABEL", "Logs")
        config = make_config(
            details_url="https://logs.example.com/c",
            definition_details_url="",
            logs_url="https://logs.example.com/c",
            definition_logs_url="",
            logs_label="Run logs",
        )
        assert config.repository == "myorg/myrepo"
        assert config.details_url == "https://logs.example.com/c"
        assert config.definition_details_url is None
        assert config.logs_url == "https://logs.example.com/c"
        assert config.definition_logs_url is None
        assert config.logs_label == "Run logs"
        # an empty render falls back to the environment
        assert GithubConfig(logs_label="").logs_label == "Logs"

    def test_empty_string_coerced_to_none(self):
        config = GithubConfig(pull_request="", repository="", logs_label="")
        assert config.pull_request is None
        assert config.repository is None
        assert config.logs_url is None
        assert config.logs_label == "View logs"

    @pytest.mark.parametrize(
        "var, field",
        [
            ("GITHUB_PULL_REQUEST", "pull_request"),
            ("GITHUB_APP_INSTALLATION_ID", "installation_id"),
        ],
    )
    def test_non_numeric_env_ignored(self, monkeypatch, var, field):
        monkeypatch.setenv(var, "not-a-number")
        assert getattr(GithubConfig(), field) is None

    def test_empty_render_coercion_leaves_required_strings_alone(self):
        config = make_config(comment_marker="none")
        assert config.comment_marker == "none"

    def test_missing_settings(self):
        errors = GithubConfig().settings_errors()
        assert errors == [
            "missing repository",
            "missing app_id",
            "missing private_key or private_key_file",
        ]

    def test_both_private_key_sources_rejected(self):
        config = make_config(private_key_file="/tmp/key.pem")
        assert config.settings_errors() == [
            "private_key and private_key_file are mutually exclusive"
        ]


class TestGithubHandlerInit:
    def test_incomplete_config_not_required_disables(self):
        handler = GithubHandler(GithubConfig())
        assert handler.is_ready() is False

    def test_incomplete_config_required_raises(self):
        with pytest.raises(HandlerError):
            GithubHandler(GithubConfig(required=True))

    def test_complete_config_is_ready(self):
        handler = GithubHandler(make_config())
        assert handler.is_ready() is True

    def test_report_and_repo_raise_before_setup(self):
        handler = GithubHandler(make_config())
        with pytest.raises(HandlerError):
            handler.report
        with pytest.raises(HandlerError):
            handler.repo

    def test_run_id_without_a_click_context(self):
        handler = GithubHandler(make_config())
        with mock.patch("click.get_current_context", side_effect=RuntimeError):
            assert handler._run_id() is None

    @pytest.mark.parametrize(
        "for_apply, commit_sha, pr_head, github_sha, expected",
        [
            (False, "configsha", "headsha", "envsha", "configsha"),
            (False, None, "headsha", "envsha", "headsha"),
            (False, None, None, "envsha", "envsha"),
            (True, "configsha", "headsha", "plannedsha", "configsha"),
            (True, None, "headsha", "plannedsha", "plannedsha"),
            (True, None, "headsha", None, "headsha"),
        ],
    )
    def test_resolve_sha(
        self, monkeypatch, for_apply, commit_sha, pr_head, github_sha, expected
    ):
        handler = make_handler(
            config=make_config(commit_sha=commit_sha), with_pr=pr_head is not None
        )
        if pr_head:
            handler._pr.head.sha = pr_head
        if github_sha:
            monkeypatch.setenv("GITHUB_SHA", github_sha)
        assert handler._resolve_sha(for_apply=for_apply) == expected

    @pytest.mark.parametrize("for_apply", [False, True])
    def test_resolve_sha_without_a_commit_raises(self, for_apply):
        handler = make_handler(with_pr=False)
        with pytest.raises(NoCommitError):
            handler._resolve_sha(for_apply=for_apply)


class TestConnect:
    @pytest.fixture
    def integration(self):
        with (
            mock.patch("github.GithubIntegration") as integration_cls,
            mock.patch("github.Auth.AppAuth") as app_auth,
        ):
            integration = integration_cls.return_value
            integration.get_repo_installation.return_value.id = 55
            yield SimpleNamespace(integration=integration, app_auth=app_auth)

    def test_discovers_installation_and_loads_pull_request(self, integration):
        handler = GithubHandler(make_config())

        handler._connect()

        integration.app_auth.assert_called_once_with("12345", "---PEM---")
        integration.integration.get_repo_installation.assert_called_once_with(
            "myorg", "myrepo"
        )
        gh = integration.integration.get_github_for_installation
        gh.assert_called_once_with(55)
        repo = gh.return_value.get_repo.return_value
        assert handler._repo is repo
        repo.get_pull.assert_called_once_with(7)
        repo.get_issue.assert_called_once_with(7)

    def test_configured_installation_and_key_file(self, integration, tmp_path):
        key = tmp_path / "app.pem"
        key.write_text("FILE PEM")
        handler = GithubHandler(
            make_config(
                private_key=None,
                private_key_file=str(key),
                installation_id=99,
                pull_request=None,
            )
        )

        handler._connect()

        integration.app_auth.assert_called_once_with("12345", "FILE PEM")
        integration.integration.get_repo_installation.assert_not_called()
        integration.integration.get_github_for_installation.assert_called_once_with(99)
        assert handler._pr is None
        assert handler._issue is None


class TestGithubStatusReport:
    def test_primary_body_has_marker_and_table(self):
        report = make_report()
        report.ensure("def1")
        report.mark("def2", "changes", plan_line="Plan: 1 to add")

        bodies = report.render_comment_bodies()

        assert len(bodies) == 1
        assert bodies[0].startswith("<!-- tfworker-status: dep -->")
        assert "| `def1` | ⏳ pending |" in bodies[0]
        assert "| `def2` | 📝 changes | Plan: 1 to add |" in bodies[0]

    def test_run_id_is_reported_when_set(self):
        """The run id keys the stored plans, so an apply can be requested for them."""
        report = make_report(run_id="run-1234")
        report.ensure("def1")

        assert "Run `run-1234`" in report.render_comment_bodies()[0]
        assert "Run `run-1234`" in report.render_check_summary()
        assert "Run `" not in make_report().render_comment_bodies()[0]

    def test_table_links_definition_to_its_check(self):
        report = make_report()
        report.ensure("linked")
        report.set_url("linked", "https://github.test/check/linked")
        report.ensure("unlinked")

        body = report.render_comment_bodies()[0]

        assert "| [`linked`](https://github.test/check/linked) |" in body
        assert "| `unlinked` |" in body

    def test_mark_preserves_full_detail(self):
        report = make_report(max_detail_chars=10)
        report.mark("def1", "changes", detail="x" * 50)
        assert report.rows["def1"].detail == "x" * 50

    def test_check_summary_detail_truncated_fence_safe(self):
        report = make_report(max_detail_chars=30)
        report.mark(
            "def1", "changes", detail="```\nline one\nline two\nline three\n```"
        )
        summary = report.render_check_summary()
        assert "_… truncated_" in summary
        assert summary.count("```") % 2 == 0
        assert "line three" not in summary

    def test_details_excluded_from_comment_by_default(self):
        report = make_report()
        report.mark(
            "def1",
            "changes",
            plan_line="Plan: 1 to add",
            detail="```\nsecret plan\n```",
        )

        bodies = report.render_comment_bodies()

        assert len(bodies) == 1
        assert "secret plan" not in bodies[0]
        assert "| `def1` | 📝 changes | Plan: 1 to add |" in bodies[0]
        # the check run output still carries the detail
        assert "secret plan" in report.render_check_summary()

    def test_oversized_detail_splits_across_comments_without_loss(self):
        report = make_report(include_details=True)
        lines = "\n".join(f"resource line {i}" for i in range(100))
        report.mark(
            "def1",
            "changes",
            plan_line="Plan: 1 to add",
            detail=f"```hcl\n{lines}\n```",
        )

        with (
            mock.patch("tfworker.handlers.github.DETAIL_CHUNK_LIMIT", 400),
            mock.patch("tfworker.handlers.github.BODY_BUDGET", 900),
        ):
            bodies = report.render_comment_bodies()

        assert len(bodies) > 2
        joined = "".join(bodies)
        for i in range(100):
            assert f"resource line {i}" in joined
        for body in bodies:
            assert body.count("```") % 2 == 0
        assert "(part 1/" in joined

    def test_comment_details_only_for_rows_with_detail(self):
        report = make_report(include_details=True)
        report.mark("def1", "changes", detail="DETAIL")
        report.mark("def2", "no_changes")
        body = report.render_comment_bodies()[0]
        assert body.count("<details>") == 1
        assert "<summary><code>def1</code>" in body

    def test_bodies_split_when_over_budget(self):
        report = make_report(max_detail_chars=400, include_details=True)
        for i in range(3):
            report.mark(f"def{i}", "changes", detail=f"detail-{i} " * 40)

        with mock.patch("tfworker.handlers.github.BODY_BUDGET", 600):
            bodies = report.render_comment_bodies()

        assert len(bodies) > 1
        assert bodies[0].startswith("<!-- tfworker-status: dep -->")
        for i, body in enumerate(bodies[1:], start=2):
            assert body.startswith(f"<!-- tfworker-status: dep part={i} -->")
        # the table stays in the primary comment only
        assert "| Definition | Status | Plan |" in bodies[0]
        assert all("| Definition | Status | Plan |" not in body for body in bodies[1:])

    @pytest.mark.parametrize("render", ["render_check_summary", "render_apply_summary"])
    def test_summary_over_budget_drops_details(self, render):
        report = make_report(max_detail_chars=500)
        report.mark("def1", "changes", detail="y" * 450)
        report.mark_apply("def1", "applied", detail="y" * 450)
        with mock.patch("tfworker.handlers.github.BODY_BUDGET", 300):
            summary = getattr(report, render)()
        assert "| `def1` |" in summary
        assert "y" * 100 not in summary
        assert "Detail sections omitted" in summary

    def test_finalize_marks_unfinished_skipped(self):
        report = make_report()
        report.ensure("never_ran")
        report.mark("in_flight", "running")
        report.mark("done", "no_changes")

        report.finalize()

        assert report.rows["never_ran"].status == "skipped"
        assert report.rows["in_flight"].status == "skipped"
        assert report.rows["done"].status == "no_changes"

    def test_conclusion(self):
        report = make_report()
        report.mark("ok", "no_changes")
        assert report.conclusion() == "success"
        report.mark("bad", "failed")
        assert report.conclusion() == "failure"

    def test_apply_column_absent_until_an_apply(self):
        report = make_report()
        report.mark("def1", "changes", plan_line="Plan: 1 to add")
        assert "| Apply |" not in report.render_comment_bodies()[0]
        report.mark_apply("def1", "applied")
        assert "| Apply |" in report.render_comment_bodies()[0]
        assert "| ✅ applied |" in report.render_comment_bodies()[0]
        # the plan check summary never shows the apply
        assert "| Apply |" not in report.render_check_summary()

    def test_headers_link_the_other_check_runs_only(self):
        report = make_report()
        report.mark("def1", "changes", plan_line="Plan: 1 to add")
        report.check_url = "https://github.test/plan"
        report.apply_check_url = "https://github.test/apply"
        report.mark_apply("def1", "applied")
        links = (
            "[View plan check run](https://github.test/plan) · "
            "[View apply check run](https://github.test/apply)"
        )
        assert links in report.render_comment_bodies()[0]
        check_summary = report.render_check_summary()
        assert "https://github.test/plan" not in check_summary
        assert "View apply check run" not in check_summary
        apply_summary = report.render_apply_summary()
        assert "[View plan check run](https://github.test/plan)" in apply_summary
        assert "https://github.test/apply" not in apply_summary

    def test_apply_summary_lists_applied_definitions(self):
        report = make_report()
        report.mark("def1", "changes", plan_line="Plan: 1 to add")
        report.ensure("untouched")
        report.mark_apply("def1", "applied", apply_line="Apply complete!", detail="LOG")
        report.set_apply_url("def1", "https://github.test/a")
        summary = report.render_apply_summary()
        assert summary.startswith("## Terraform apply status: `dep`")
        assert "| [`def1`](https://github.test/a) | ✅ applied | Apply complete! |" in (
            summary
        )
        assert "untouched" not in summary
        assert "LOG" in summary

    def test_apply_conclusion_scoped_to_named_definitions(self):
        report = make_report()
        report.mark_apply("old", "failed")
        report.mark_apply("def1", "applied")
        assert report.apply_conclusion(["def1"]) == "success"
        assert report.apply_conclusion(["def1", "old"]) == "failure"

    def test_apply_summary_line(self):
        report = make_report()
        report.mark("def1", "changes", plan_line="Plan: 1 to add")
        assert report.apply_summary_line() == "no definitions"
        report.mark_apply("def1", "applied")
        assert report.apply_summary_line() == "1 ✅ applied"


class TestMarkdownHelpers:
    @pytest.mark.parametrize(
        "text, width",
        [
            ('resource "aws_s3_bucket" "b" {\n  bucket = "short"\n}', 120),
            # zero width disables wrapping
            ("x" * 300, 0),
        ],
    )
    def test_hard_wrap_leaves_text_untouched(self, text, width):
        assert _hard_wrap(text, width) == text

    def test_long_line_wrapped_with_indent(self):
        line = "      query = " + "sum:metric{tag} " * 20
        out = _hard_wrap(line, 80)
        lines = out.splitlines()
        assert len(lines) > 1
        assert all(len(ln) <= 80 for ln in lines)
        assert all(ln.startswith("      ") for ln in lines)
        assert lines[1].startswith("          ")

    def test_unbreakable_token_left_intact(self):
        arn = "arn:aws:iam::123456789012:role/" + "a" * 150
        assert arn in _hard_wrap(f"role = {arn}", 80)

    def test_markers_hoisted_to_column_zero(self):
        text = (
            '  + resource "a" "b" {\n'
            '  - resource "c" "d" {\n'
            '  ~ resource "e" "f" {\n'
            '-/+ resource "g" "h" {\n'
            "  # comment line\n"
            "      + attr = 1\n"
        )
        out = _diff_format(text).splitlines()
        assert out[0].startswith("+  ")
        assert out[1].startswith("-  ")
        assert out[2].startswith("!  ")
        # -/+ has no leading indent, left alone
        assert out[3].startswith("-/+")
        assert out[4] == "  # comment line"
        assert out[5].startswith("+      ")

    def test_replace_marker_with_indent_becomes_bang(self):
        assert _diff_format('  -/+ resource "g" "h" {').startswith("!  ")

    @pytest.mark.parametrize(
        "helper, expected",
        [(_chunk_markdown, ["short"]), (_fence_safe_truncate, "short")],
    )
    def test_under_limit_is_a_noop(self, helper, expected):
        assert helper("short", 100) == expected

    def test_chunk_markdown_balances_and_reopens_fences(self):
        text = "```hcl\n" + "\n".join(f"line {i}" for i in range(50)) + "\n```"
        chunks = _chunk_markdown(text, 100)
        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.count("```") % 2 == 0
        for chunk in chunks[1:]:
            assert chunk.startswith("```hcl")
        joined = "".join(chunks)
        for i in range(50):
            assert f"line {i}" in joined

    def test_fence_safe_truncate_closes_open_fence(self):
        text = "```\n" + "x" * 200
        out = _fence_safe_truncate(text, 50)
        assert out.count("```") % 2 == 0
        assert "_… truncated_" in out


class TestOutputParsing:
    def test_trimmed_plan_wraps_long_lines(self):
        text = (
            "Terraform will perform the following actions:\n"
            + "  attribute = "
            + "value " * 40
            + "\n"
            + "Plan: 1 to add, 0 to change, 0 to destroy.\n"
        )
        out = GithubHandler._trimmed_plan(text, wrap_width=60)
        assert all(
            len(line) <= 60 for line in out.splitlines() if not line.startswith("```")
        )
        assert out.count("value") == 40

    def test_output_only_plan_is_captured(self):
        """A plan that only moves outputs has neither the resource-actions
        heading nor a Plan: line, and used to render as nothing at all."""
        text = (
            "Terraform used the selected providers to generate the following\n"
            "execution plan.\n"
            "\n"
            "Changes to Outputs:\n"
            '  + oidc_provider_arns = "known after apply"\n'
            "\n"
            "You can apply this plan to save these new output values to the\n"
            "Terraform state, without changing any real infrastructure.\n"
        )
        out = GithubHandler._trimmed_plan(text)
        assert out.startswith("```diff\n")
        assert "Changes to Outputs:" in out
        assert "oidc_provider_arns" in out
        assert "without changing any real infrastructure" not in out

    def test_output_changes_after_plan_line_are_kept(self):
        """Output changes follow the Plan: line; capture used to stop there."""
        text = (
            "Terraform will perform the following actions:\n"
            '  + resource "null_resource" "example" {\n'
            "Plan: 1 to add, 0 to change, 0 to destroy.\n"
            "\n"
            "Changes to Outputs:\n"
            '  + example = "value"\n'
        )
        out = GithubHandler._trimmed_plan(text)
        assert "Plan: 1 to add" in out
        assert "Changes to Outputs:" in out
        assert "example" in out

    def test_trimmed_plan_stops_at_saved_plan_footer(self):
        ruler = "─" * 20
        text = (
            "Terraform will perform the following actions:\n"
            '  + resource "null_resource" "example" {\n'
            "Plan: 1 to add, 0 to change, 0 to destroy.\n"
            "\n"
            f"{ruler}\n"
            "\n"
            "Saved the plan to: plan.tfplan\n"
            "\n"
            "To perform exactly these actions, run the following command to apply:\n"
            '    terraform apply "plan.tfplan"\n'
        )
        out = GithubHandler._trimmed_plan(text)
        assert "Plan: 1 to add" in out
        assert "Saved the plan to" not in out
        assert "terraform apply" not in out
        assert "─" not in out

    def test_trimmed_plan_uses_diff_fence(self):
        text = (
            "Terraform will perform the following actions:\n"
            '  + resource "null_resource" "example" {\n'
            "Plan: 1 to add, 0 to change, 0 to destroy.\n"
        )
        out = GithubHandler._trimmed_plan(text)
        assert out.startswith("```diff\n")
        assert '\n+   resource "null_resource" "example" {\n' in out

    @pytest.mark.parametrize(
        "text, expected",
        [
            ('Changes to Outputs:\n  + example = "value"\n', "Output changes only."),
            (
                "Plan: 1 to add, 0 to change, 0 to destroy.\n\nChanges to Outputs:\n",
                "Plan: 1 to add, 0 to change, 0 to destroy.",
            ),
            ("No changes. Your infrastructure matches.\n", ""),
        ],
    )
    def test_plan_line(self, text, expected):
        assert GithubHandler._plan_line(text) == expected

    def test_apply_output_collapses_blank_runs(self):
        out = GithubHandler._apply_output(APPLY_STDOUT.decode())
        assert out.startswith("```\n")
        assert "\n\n\n" not in out
        assert GithubHandler._apply_output("\n\n") == ""

    def test_apply_line_absent(self):
        assert GithubHandler._apply_line("nothing here") == ""

    def test_trimmed_plan_without_changes_is_empty(self):
        assert GithubHandler._trimmed_plan("No changes. Infrastructure matches.") == ""


class TestPlanExecute:
    def test_unhandled_action_is_ignored(self):
        handler = make_handler()
        ret = execute(
            handler, TerraformAction.DESTROY, POST, TerraformResult(0, b"", b"")
        )
        assert ret is None
        handler._issue.create_comment.assert_not_called()

    def test_not_ready_handler_does_nothing(self):
        handler = make_handler()
        handler._ready = False
        ret = execute(handler, PLAN, POST, TerraformResult(2, PLAN_STDOUT, b""))
        assert ret is None
        handler._issue.create_comment.assert_not_called()

    @pytest.mark.parametrize("action", [PLAN, APPLY])
    def test_post_without_result_is_ignored(self, action):
        handler = make_handler()
        assert execute(handler, action, POST) is None
        handler._plan_checks.checks["mydef"].edit.assert_not_called()
        handler._repo.create_check_run.assert_not_called()

    def test_pre_plan_marks_running(self):
        handler = make_handler()
        execute(handler, PLAN, PRE)
        assert handler._report.rows["mydef"].status == "running"
        handler._issue.create_comment.assert_called_once()
        handler._plan_checks.checks["mydef"].edit.assert_called_once_with(
            status="in_progress"
        )

    @pytest.mark.parametrize(
        "stage, result, status, plan_line, conclusion, title, detail",
        [
            (
                POST,
                NO_CHANGES,
                "no_changes",
                "No changes.",
                "success",
                "No changes.",
                "",
            ),
            (
                POST,
                TerraformResult(2, PLAN_STDOUT, b""),
                "changes",
                "Plan: 1 to add, 0 to change, 0 to destroy.",
                "success",
                "Plan: 1 to add, 0 to change, 0 to destroy.",
                "null_resource.example",
            ),
            (
                POST,
                TerraformResult(1, b"", b"Error: something broke\n"),
                "failed",
                "",
                "failure",
                "Terraform plan failed",
                "something broke",
            ),
            (
                ERROR,
                TerraformResult(1, b"", b"Error: bad provider\n"),
                "failed",
                "",
                "failure",
                "Terraform plan failed",
                "bad provider",
            ),
        ],
        ids=["no_changes", "changes", "failed", "error_stage"],
    )
    def test_plan_outcome(
        self, stage, result, status, plan_line, conclusion, title, detail
    ):
        handler = make_handler()

        ret = execute(handler, PLAN, stage, result)

        row = handler._report.rows["mydef"]
        assert row.status == status
        assert row.plan_line == plan_line
        assert detail in row.detail
        kwargs = handler._plan_checks.checks["mydef"].edit.call_args.kwargs
        assert kwargs["status"] == "completed"
        assert kwargs["conclusion"] == conclusion
        assert kwargs["output"]["title"] == title
        assert detail in kwargs["output"]["summary"]
        handler._plan_checks.rollup.edit.assert_called_once()
        if stage == POST:
            assert ret.handler == "github"
            assert ret.definition == "mydef"
            assert ret.definition_check_url == "https://github.test/check/mydef"
        else:
            assert ret is None

    def test_post_plan_prefers_openai_summary(self):
        handler = make_handler()
        handler._app_state.handlers.get_results.return_value = [
            SimpleNamespace(
                task="summary",
                file="/tmp/plans/mydef.tfplan.summary.md",
                content="AI SUMMARY OF PLAN",
            ),
            SimpleNamespace(
                task="summary",
                file="/tmp/plans/otherdef.tfplan.summary.md",
                content="WRONG DEFINITION",
            ),
        ]
        execute(handler, PLAN, POST, TerraformResult(2, PLAN_STDOUT, b""))
        detail = handler._report.rows["mydef"].detail
        assert "AI SUMMARY OF PLAN" in detail
        assert "WRONG DEFINITION" not in detail

    def test_summary_lookup_errors_fall_back_to_the_plan(self):
        handler = make_handler()
        handler._app_state.handlers.get_results.side_effect = RuntimeError("boom")
        execute(handler, PLAN, POST, TerraformResult(2, PLAN_STDOUT, b""))
        assert handler._report.rows["mydef"].detail.startswith("```diff\n")

    def test_summary_lookup_without_a_plan_file(self):
        handler = make_handler()
        result = TerraformResult(2, PLAN_STDOUT, b"")
        execute(handler, PLAN, POST, result, plan_file=None)
        assert handler._report.rows["mydef"].detail.startswith("```diff\n")
        handler._app_state.handlers.get_results.assert_not_called()

    def test_plan_stages_without_plan_checks(self):
        """A plan-destroy apply run plans without the plan check runs of a plan run."""
        handler = make_handler()
        handler._plan_checks.rollup = None
        handler._plan_checks.checks = {}
        ret = execute(handler, PLAN, POST, NO_CHANGES)
        assert ret.check_run_url is None
        assert ret.definition_check_url is None
        assert handler._api_failures == 0
        assert handler._report.rows["mydef"].status == "no_changes"

    def test_no_pr_skips_comments_but_updates_check(self):
        handler = make_handler(with_pr=False)
        execute(handler, PLAN, POST, TerraformResult(2, PLAN_STDOUT, b""))
        assert handler._comments == []
        handler._plan_checks.rollup.edit.assert_called_once()

    @staticmethod
    def failing_api(handler, api):
        return {
            "check": handler._plan_checks.checks["mydef"].edit,
            "comment": handler._issue.create_comment,
        }[api]

    @pytest.mark.parametrize(
        "api, ready, comments_enabled",
        [("check", False, True), ("comment", True, False)],
    )
    def test_failures_disable_after_threshold(self, api, ready, comments_enabled):
        handler = make_handler()
        self.failing_api(handler, api).side_effect = RuntimeError("boom")
        for _ in range(3):
            execute(handler, PLAN, PRE)
        assert handler.is_ready() is ready
        assert handler._comments_enabled is comments_enabled

    def test_check_runs_keep_reporting_after_the_comment_gives_up(self):
        handler = make_handler()
        handler._disable_comments("gave up")
        execute(handler, PLAN, POST, NO_CHANGES)
        handler._plan_checks.checks["mydef"].edit.assert_called_once()
        handler._issue.create_comment.assert_not_called()

    @pytest.mark.parametrize(
        "api, effects, counter, expected",
        [
            ("check", ["boom", "boom", None, "boom"], "_api_failures", 1),
            ("comment", ["boom", "boom", mock.DEFAULT], "_comment_failures", 0),
        ],
    )
    def test_success_resets_the_failure_count(self, api, effects, counter, expected):
        handler = make_handler()
        self.failing_api(handler, api).side_effect = [
            RuntimeError(e) if e == "boom" else e for e in effects
        ]
        for _ in effects:
            execute(handler, PLAN, PRE)
        assert getattr(handler, counter) == expected
        assert handler.is_ready() is True
        assert handler._comments_enabled is True


class TestApply:
    def apply_handler(self, planning=True):
        handler = make_handler()
        handler._planning = planning
        handler._app_state.root_options.run_id = "run-1234"
        rollup, per_def = apply_checks(handler)
        return handler, rollup, per_def

    def test_pre_apply_creates_rollup_and_definition_checks(self):
        handler, rollup, per_def = self.apply_handler()
        handler._report.mark("mydef", "changes", plan_line="Plan: 1 to add")

        execute(handler, APPLY, PRE)

        calls = handler._repo.create_check_run.call_args_list
        assert calls[0].kwargs["name"] == "tfworker/dep/apply"
        assert calls[0].kwargs["external_id"] == "run-1234"
        assert calls[1].kwargs["name"] == "tfworker/dep/apply: mydef"
        assert calls[1].kwargs["head_sha"] == "abc123"
        assert calls[1].kwargs["status"] == "in_progress"
        row = handler._report.rows["mydef"]
        assert row.apply_status == "running"
        assert row.apply_url == "https://github.test/apply/mydef"
        body = handler._issue.create_comment.call_args.args[0]
        assert "| Apply |" in body
        assert "[🚀 applying](https://github.test/apply/mydef)" in body
        assert "[View apply check run](https://github.test/apply)" in body

    def test_rollup_created_once(self):
        handler, rollup, per_def = self.apply_handler()
        other = mock.Mock(html_url="https://github.test/apply/other")
        handler._repo.create_check_run.side_effect = [rollup, per_def, other]
        execute(handler, APPLY, PRE)
        execute(handler, APPLY, PRE, name="other")
        names = [
            c.kwargs["name"] for c in handler._repo.create_check_run.call_args_list
        ]
        assert names.count("tfworker/dep/apply") == 1

    def test_post_apply_concludes_success_and_keeps_applied_plan(self):
        handler, rollup, per_def = self.apply_handler()
        handler._report.mark("mydef", "changes", detail="```diff\n+ planned\n```")
        execute(handler, APPLY, PRE)

        ret = execute(handler, APPLY, POST, TerraformResult(0, APPLY_STDOUT, b""))

        kwargs = per_def.edit.call_args.kwargs
        assert kwargs["conclusion"] == "success"
        assert kwargs["output"]["title"] == (
            "Apply complete! Resources: 1 added, 0 changed, 0 destroyed."
        )
        summary = kwargs["output"]["summary"]
        # headed by a link back to the apply rollup
        assert summary.startswith(
            "[View dep apply check run](https://github.test/apply)\n\n```\n"
        )
        assert "Creation complete" in summary
        assert "### Applied plan" in kwargs["output"]["text"]
        assert "+ planned" in kwargs["output"]["text"]
        assert handler._report.rows["mydef"].apply_status == "applied"
        assert ret.action == TerraformAction.APPLY
        assert ret.check_run_url == "https://github.test/apply"
        assert ret.definition_check_url == "https://github.test/apply/mydef"

    def test_error_stage_concludes_failure(self):
        handler, rollup, per_def = self.apply_handler()
        execute(handler, APPLY, PRE)

        execute(handler, APPLY, ERROR, TerraformResult(1, b"", b"Error: apply broke\n"))

        kwargs = per_def.edit.call_args.kwargs
        assert kwargs["conclusion"] == "failure"
        assert "apply broke" in kwargs["output"]["summary"]
        assert "text" not in kwargs["output"]
        assert handler._report.rows["mydef"].apply_status == "failed"

    def test_pre_apply_without_plan_state_reports_changes(self):
        """An apply-only run that could not read the plan back still knows a
        definition with a stored plan planned changes."""
        handler, rollup, per_def = self.apply_handler(planning=False)
        execute(handler, APPLY, PRE)
        assert handler._report.rows["mydef"].status == "changes"

    def test_teardown_concludes_apply_rollup(self):
        handler, rollup, per_def = self.apply_handler()
        handler._report.mark("mydef", "changes")
        handler._report.mark("never_applied", "changes")
        handler._report.mark("unchanged", "no_changes")
        execute(handler, APPLY, PRE)
        execute(handler, APPLY, ERROR, TerraformResult(1, b"", b"Error: x\n"))

        handler.teardown("dep", "/tmp")

        kwargs = rollup.edit.call_args.kwargs
        assert kwargs["status"] == "completed"
        assert kwargs["conclusion"] == "failure"
        rows = handler._report.rows
        assert rows["never_applied"].apply_status == "not_applied"
        assert rows["unchanged"].apply_status is None

    def test_teardown_skips_unconcluded_apply_checks(self):
        handler, rollup, per_def = self.apply_handler()
        execute(handler, APPLY, PRE)

        handler.teardown("dep", "/tmp")

        kwargs = per_def.edit.call_args.kwargs
        assert kwargs["conclusion"] == "skipped"
        assert handler._report.rows["mydef"].apply_status == "not_applied"
        assert rollup.edit.call_args.kwargs["conclusion"] == "success"

    @pytest.mark.parametrize("failing, other", [("plan", "apply"), ("apply", "plan")])
    def test_teardown_blocks_are_isolated(self, failing, other):
        handler, rollup, per_def = self.apply_handler()
        execute(handler, APPLY, PRE)
        rollups = {"plan": handler._plan_checks.rollup, "apply": rollup}
        rollups[failing].edit.side_effect = RuntimeError("boom")

        with mock.patch("tfworker.handlers.github.log.error") as error:
            handler.teardown("dep", "/tmp")

        error.assert_called_once()
        assert f"{failing} teardown failed" in error.call_args.args[0]
        assert rollups[other].edit.call_args.kwargs["status"] == "completed"


class TestSetup:
    def test_run_id_tags_the_check_runs(self):
        """The run id lets a tool request an apply of this run's plans, and an apply match them."""
        handler = make_handler()
        handler._app_state.root_options.run_id = "run-1234"
        run_setup(handler)
        rollup, per_def = handler._repo.create_check_run.call_args_list
        assert rollup.kwargs["external_id"] == "run-1234"
        assert rollup.kwargs["name"] == "tfworker/dep/plan"
        assert "Run `run-1234`" in rollup.kwargs["output"]["summary"]
        assert per_def.kwargs["name"] == "tfworker/dep/plan: mydef"
        assert per_def.kwargs["external_id"] == "run-1234"

    def test_external_id_omitted_without_a_run_id(self):
        handler = make_handler()
        run_setup(handler)
        created = handler._repo.create_check_run.call_args_list
        assert created
        assert all("external_id" not in c.kwargs for c in created)

    def test_apply_only_rebuilds_state_and_creates_no_plan_checks(self):
        handler = make_handler()
        handler._app_state.root_options.run_id = "run-1234"
        handler._load_run_state = mock.Mock()
        run_setup(handler, plan=False, apply=True)
        assert handler.is_ready() is True
        assert handler._planning is False
        handler._resolve_sha.assert_called_once_with(for_apply=True)
        handler._load_run_state.assert_called_once_with("run-1234")
        handler._repo.create_check_run.assert_not_called()

    def test_claim_failure_disables_comments_not_the_handler(self):
        handler = make_handler()
        handler._issue.get_comments.side_effect = RuntimeError("boom")
        run_setup(handler)
        assert handler.is_ready() is True
        assert handler._comments_enabled is False
        handler._issue.create_comment.assert_not_called()
        handler._repo.create_check_run.assert_called()

    def test_failure_disables_the_handler(self):
        handler = make_handler()
        handler._repo.create_check_run.side_effect = RuntimeError("bad credentials")
        with mock.patch("tfworker.handlers.github.log.error") as error:
            run_setup(handler, definitions={})
        assert handler.is_ready() is False
        assert "bad credentials" in error.call_args.args[0]

    def test_connect_failure_logs_an_error(self):
        handler = make_handler()
        with mock.patch("tfworker.handlers.github.log.error") as error:
            handler._connect = mock.Mock(side_effect=RuntimeError("auth failed"))
            handler.setup("dep", {}, "/tmp", SimpleNamespace(plan=True, apply=False))
        assert handler.is_ready() is False
        assert "auth failed" in error.call_args.args[0]

    @pytest.mark.parametrize("plan, apply", [(True, False), (False, True)])
    def test_no_commit_skips_without_an_error(self, plan, apply):
        handler = make_handler(with_pr=False)
        handler._connect = mock.Mock()
        with (
            mock.patch("tfworker.handlers.github.log.error") as error,
            mock.patch("tfworker.handlers.github.log.info") as info,
        ):
            handler.setup(
                "dep",
                {"mydef": make_definition()},
                "/tmp",
                SimpleNamespace(plan=plan, apply=apply),
            )
        error.assert_not_called()
        info.assert_called_once_with(
            "github handler: no commit to report on; set commit_sha or GITHUB_SHA; skipping"
        )
        assert handler.is_ready() is False
        assert execute(handler, PLAN, PRE) is None
        handler.teardown("dep", "/tmp")
        handler._repo.create_check_run.assert_not_called()
        handler._plan_checks.rollup.edit.assert_not_called()

    def test_no_commit_raises_when_required(self):
        handler = make_handler(config=make_config(required=True), with_pr=False)
        handler._connect = mock.Mock()
        with pytest.raises(HandlerError, match="no commit to report on"):
            handler.setup("dep", {}, "/tmp", SimpleNamespace(plan=True, apply=False))
        assert handler.is_ready() is False

    def test_failure_raises_when_required(self):
        handler = make_handler(config=make_config(required=True))
        handler._repo.create_check_run.side_effect = RuntimeError("bad credentials")
        with pytest.raises(HandlerError, match="bad credentials"):
            run_setup(handler, definitions={})

    def test_setup_skipped_when_not_ready(self):
        handler = GithubHandler(GithubConfig())
        run_setup(handler)
        handler._connect.assert_not_called()
        assert handler._report is None

    def test_neither_plan_nor_apply_disables(self):
        handler = make_handler()
        run_setup(handler, plan=False, apply=False)
        assert handler.is_ready() is False
        handler._connect.assert_not_called()


class TestTeardown:
    def test_teardown_concludes_check_run_failure(self):
        handler = make_handler()
        handler._report.mark("mydef", "failed", detail="boom")

        handler.teardown("dep", "/tmp")

        kwargs = handler._plan_checks.rollup.edit.call_args.kwargs
        assert kwargs["status"] == "completed"
        assert kwargs["conclusion"] == "failure"

    def test_teardown_concludes_success_and_skips_pending(self):
        handler = make_handler()
        handler._report.mark("mydef", "no_changes")
        handler._report.ensure("never_ran")

        handler.teardown("dep", "/tmp")

        kwargs = handler._plan_checks.rollup.edit.call_args.kwargs
        assert kwargs["conclusion"] == "success"
        assert handler._report.rows["never_ran"].status == "skipped"

    def test_teardown_without_setup_is_noop(self):
        handler = make_handler()
        handler._report = None
        handler.teardown("dep", "/tmp")
        handler._plan_checks.rollup.edit.assert_not_called()

    def test_apply_only_teardown_without_applies_leaves_comment(self):
        handler = make_handler()
        handler._planning = False
        handler._comments = [mock.Mock(body="old")]

        handler.teardown("dep", "/tmp")

        handler._comments[0].edit.assert_not_called()
        handler._plan_checks.rollup.edit.assert_not_called()


class TestComments:
    """Claiming is scoped to the deployment: several deployments report to one
    PR and each owns its own comments."""

    def test_claims_only_this_deployments_comments_primary_first(self):
        handler = make_handler()
        handler._report = make_report(deployment="web-prod")
        mine = "<!-- tfworker-status: web-prod -->\n## status"
        mine_part = "<!-- tfworker-status: web-prod part=2 -->\ndetail"
        theirs = "<!-- tfworker-status: web-staging -->\n## status"
        theirs_part = "<!-- tfworker-status: web-staging part=2 -->\ndetail"
        bodies = [mine_part, theirs, "just a human comment", "", theirs_part, mine]
        handler._issue.get_comments.return_value = [mock.Mock(body=b) for b in bodies]

        handler._claim_comments()

        assert [c.body for c in handler._comments] == [mine, mine_part]

    def test_does_not_claim_a_deployment_it_merely_prefixes(self):
        handler = make_handler()
        handler._report = make_report(deployment="web")
        handler._issue.get_comments.return_value = [
            mock.Mock(body="<!-- tfworker-status: web-staging -->\n## status")
        ]

        handler._claim_comments()

        assert handler._comments == []

    def test_noop_without_report_or_issue(self):
        handler = make_handler()
        handler._report = None
        handler._claim_comments()
        assert handler._comments == []

    def test_update_comments_edits_creates_and_deletes(self):
        handler = make_handler()
        existing_primary = mock.Mock(body="old primary")
        stale_part = mock.Mock(body="old part 2")
        stale_part_2 = mock.Mock(body="old part 3")
        handler._comments = [existing_primary, stale_part, stale_part_2]
        handler._report.mark("mydef", "changes", detail="small detail")

        handler._update_comments()

        existing_primary.edit.assert_called_once()
        # single body now: both stale continuation comments removed
        stale_part.delete.assert_called_once()
        stale_part_2.delete.assert_called_once()
        assert len(handler._comments) == 1

    def test_update_comments_skips_edit_when_unchanged(self):
        handler = make_handler()
        body = handler._report.render_comment_bodies()[0]
        existing = mock.Mock(body=body)
        handler._comments = [existing]

        handler._update_comments()

        existing.edit.assert_not_called()

    def test_disable_without_a_pr_does_not_warn(self):
        handler = make_handler(with_pr=False)
        with mock.patch("tfworker.handlers.github.log.warn") as warn:
            handler._disable_comments("reason")
        warn.assert_not_called()
        assert handler._comments_enabled is False


class TestLoadRunState:
    def handler_with_runs(self, *runs):
        handler = make_handler()
        handler._planning = False
        handler._report = make_report(max_detail_chars=8000, run_id="run-1")
        handler._repo.get_commit.return_value.get_check_runs.return_value = list(runs)
        return handler

    def test_rebuilds_rows_from_this_runs_checks(self):
        handler = self.handler_with_runs(
            check_run(10, "tfworker/dep/plan", external_id="run-1"),
            check_run(
                11, "tfworker/dep/plan: a", title="No changes.", external_id="run-1"
            ),
            check_run(
                12,
                "tfworker/dep/plan: b",
                title="Plan: 1 to add",
                summary="DIFF",
                external_id="run-1",
            ),
            check_run(
                13, "tfworker/dep/plan: c", conclusion="failure", external_id="run-1"
            ),
            check_run(
                14, "tfworker/dep/plan: d", conclusion="skipped", external_id="run-1"
            ),
            check_run(
                15, "tfworker/dep/plan: e", title="Changes planned", external_id="run-1"
            ),
            # an unfinished check reads back as pending
            check_run(16, "tfworker/dep/plan: f", conclusion=None, external_id="run-1"),
            # another run's checks on the same commit are ignored
            check_run(20, "tfworker/dep/plan", external_id="run-2"),
            check_run(21, "tfworker/dep/plan: z", external_id="run-2"),
        )

        handler._load_run_state("run-1")

        rows = handler._report.rows
        assert list(rows) == ["a", "b", "c", "d", "e", "f"]
        assert rows["a"].status == "no_changes"
        assert rows["b"].status == "changes"
        assert rows["b"].plan_line == "Plan: 1 to add"
        assert rows["b"].detail == "DIFF"
        assert rows["b"].url == "https://github.test/runs/12"
        assert rows["c"].status == "failed"
        assert rows["d"].status == "skipped"
        assert rows["e"].plan_line == ""
        assert rows["f"].status == "pending"
        assert rows["f"].plan_line == ""
        assert handler._report.check_url == "https://github.test/runs/10"
        get_runs = handler._repo.get_commit.return_value.get_check_runs
        get_runs.assert_called_once_with(filter="all")
        assert handler._comments_enabled is True

    def test_legacy_definition_checks_matched_by_creation_order(self):
        handler = self.handler_with_runs(
            check_run(5, "tfworker/dep/plan: stale", title="Plan: old"),
            check_run(10, "tfworker/dep/plan", external_id="run-1"),
            check_run(11, "tfworker/dep/plan: a", title="Plan: 1 to add"),
        )
        handler._load_run_state("run-1")
        assert list(handler._report.rows) == ["a"]

    def test_newest_check_wins_keeping_position(self):
        handler = self.handler_with_runs(
            check_run(10, "tfworker/dep/plan", external_id="run-1"),
            check_run(
                11, "tfworker/dep/plan: a", conclusion="failure", external_id="run-1"
            ),
            check_run(12, "tfworker/dep/plan: b", external_id="run-1"),
            check_run(
                13, "tfworker/dep/plan: a", title="Plan: 2 to add", external_id="run-1"
            ),
        )
        handler._load_run_state("run-1")
        rows = handler._report.rows
        assert list(rows) == ["a", "b"]
        assert rows["a"].status == "changes"

    def test_prior_applies_of_the_run_are_loaded(self):
        handler = self.handler_with_runs(
            check_run(10, "tfworker/dep/plan", external_id="run-1"),
            check_run(11, "tfworker/dep/plan: a", title="Plan: 1", external_id="run-1"),
            check_run(12, "tfworker/dep/plan: b", title="Plan: 1", external_id="run-1"),
            check_run(13, "tfworker/dep/plan: c", title="Plan: 1", external_id="run-1"),
            check_run(14, "tfworker/dep/plan: d", title="Plan: 1", external_id="run-1"),
            check_run(
                20,
                "tfworker/dep/apply: a",
                title="Apply complete!",
                external_id="run-1",
            ),
            check_run(
                21,
                "tfworker/dep/apply: b",
                conclusion="failure",
                title="Terraform apply failed",
                external_id="run-1",
            ),
            check_run(
                22, "tfworker/dep/apply: c", conclusion="skipped", external_id="run-1"
            ),
            check_run(
                23, "tfworker/dep/apply: d", conclusion=None, external_id="run-1"
            ),
            check_run(
                24, "tfworker/dep/apply: a", title="ignored", external_id="run-2"
            ),
        )
        handler._load_run_state("run-1")
        rows = handler._report.rows
        assert rows["a"].apply_status == "applied"
        assert rows["a"].apply_line == "Apply complete!"
        assert rows["a"].apply_url == "https://github.test/runs/20"
        assert rows["b"].apply_status == "failed"
        assert rows["b"].apply_line == ""
        assert rows["c"].apply_status == "not_applied"
        assert rows["d"].apply_status is None

    def test_links_are_stripped_when_read_back(self):
        handler = self.handler_with_runs(
            check_run(10, "tfworker/dep/plan", external_id="run-1"),
            check_run(
                11,
                "tfworker/dep/plan: a",
                title="Plan: 1 to add",
                summary="[View logs](https://l/a) · [View dep plan check run](https://p)"
                "\n\nDIFF",
                external_id="run-1",
            ),
            check_run(
                12,
                "tfworker/dep/plan: b",
                title="No changes.",
                summary="[View logs](https://l/b)",
                external_id="run-1",
            ),
            check_run(
                13,
                "tfworker/dep/plan: c",
                title="Plan: 1 to add",
                summary="[Other](https://x)\n\nKEEP",
                external_id="run-1",
            ),
            check_run(
                20,
                "tfworker/dep/apply: a",
                title="Apply complete!",
                summary="[View dep apply check run](https://a)\n\nLOG",
                external_id="run-1",
            ),
        )
        handler._load_run_state("run-1")
        rows = handler._report.rows
        assert rows["a"].detail == "DIFF"
        assert rows["b"].detail == ""
        assert rows["c"].detail == "[Other](https://x)\n\nKEEP"
        assert rows["a"].apply_detail == "LOG"

    def test_claims_comment_of_this_run(self):
        handler = self.handler_with_runs(
            check_run(10, "tfworker/dep/plan", external_id="run-1"),
        )
        mine = mock.Mock(body="<!-- tfworker-status: dep -->\nRun `run-1`\n")
        handler._issue.get_comments.return_value = [mine]
        handler._load_run_state("run-1")
        assert handler._comments == [mine]
        assert handler._comments_enabled is True

    def test_comment_of_another_run_is_left_alone(self):
        handler = self.handler_with_runs(
            check_run(10, "tfworker/dep/plan", external_id="run-1"),
        )
        newer = mock.Mock(body="<!-- tfworker-status: dep -->\nRun `run-2`\n")
        handler._issue.get_comments.return_value = [newer]

        handler._load_run_state("run-1")
        handler._update_comments()

        assert handler._comments_enabled is False
        newer.edit.assert_not_called()

    def test_no_plan_rollup_disables_comments(self):
        handler = self.handler_with_runs(
            check_run(10, "tfworker/dep/plan", external_id="run-2"),
        )
        handler._load_run_state("run-1")
        assert handler._comments_enabled is False
        assert handler._report.rows == {}

    def test_no_run_id_disables_comments(self):
        handler = self.handler_with_runs()
        handler._load_run_state(None)
        assert handler._comments_enabled is False
        handler._repo.get_commit.assert_not_called()

    def test_read_failure_disables_comments_not_the_handler(self):
        handler = self.handler_with_runs()
        handler._repo.get_commit.side_effect = RuntimeError("boom")
        handler._load_run_state("run-1")
        assert handler._comments_enabled is False
        assert handler.is_ready() is True


LINK_TEMPLATE = (
    "https://logs.example.com/search?q=run%3A{run_id}%20dep%3A{deployment}"
    "%20def%3A{definition}&from={from_ts}&to={to_ts}"
)


def details(definition="", from_ts=700000, to_ts=22600000, run_id="run-1234"):
    """The expected LINK_TEMPLATE url; defaults are a clock started and read at 1000s."""
    return (
        f"https://logs.example.com/search?q=run%3A{run_id}%20dep%3Adep"
        f"%20def%3A{definition}&from={from_ts}&to={to_ts}"
    )


def logs(definition="", from_ts=700000, to_ts=22600000, label="View logs"):
    """The expected logs link line; defaults are a clock started and read at 1000s."""
    return f"[{label}]({details(definition, from_ts, to_ts)})"


def links_handler(clock=None, **config):
    """A handler with both link templates set, for a run with an id."""
    config.setdefault("details_url", LINK_TEMPLATE)
    config.setdefault("logs_url", LINK_TEMPLATE)
    handler = make_handler(config=make_config(**config), clock=clock)
    handler._app_state.root_options.run_id = "run-1234"
    return handler


class TestCheckRunLinks:
    """Check runs link to the run's logs as a details url and a labelled output link:
    while running, a window open well past creation; once concluded, exactly the run."""

    def test_plan_checks_created_with_a_running_window(self):
        clock = FakeClock(500.0)
        handler = links_handler(clock=clock)
        clock.now = 1000.0  # setup time, not construction time, starts the window
        run_setup(handler)

        rollup, per_def = handler._repo.create_check_run.call_args_list
        assert rollup.kwargs["details_url"] == details()
        assert per_def.kwargs["details_url"] == details(definition="mydef")
        assert f"Run `run-1234`\n\n{logs()}\n" in rollup.kwargs["output"]["summary"]
        # a check created without output gets none just for the link
        assert "output" not in per_def.kwargs

    @pytest.mark.parametrize(
        "details_url, expected",
        [
            (
                "https://docs.example.com/{deployment}",
                {"details_url": "https://docs.example.com/dep"},
            ),
            (None, {}),
        ],
    )
    def test_logs_template_leaves_details_url_alone(self, details_url, expected):
        handler = links_handler(details_url=details_url)
        run_setup(handler)
        rollup, per_def = handler._repo.create_check_run.call_args_list
        assert logs() in rollup.kwargs["output"]["summary"]
        for call in (rollup, per_def):
            assert {k: v for k, v in call.kwargs.items() if k == "details_url"} == (
                expected
            )

    def test_plan_checks_concluded_with_the_run_window(self):
        clock = FakeClock()
        handler = links_handler(clock=clock)
        clock.now = 1060.0

        execute(handler, PLAN, POST, TerraformResult(2, PLAN_STDOUT, b""))
        def_kwargs = handler._plan_checks.checks["mydef"].edit.call_args.kwargs
        assert def_kwargs["details_url"] == details("mydef", to_ts=1360000)
        assert def_kwargs["output"]["summary"].startswith(
            f"{logs('mydef', to_ts=1360000)}\n\n```diff\n"
        )
        # the rollup re-render while running keeps the open window
        running = handler._plan_checks.rollup.edit.call_args.kwargs["output"]["summary"]
        assert logs(to_ts=22660000) in running

        clock.now = 1120.0
        handler.teardown("dep", "/tmp")
        rollup_kwargs = handler._plan_checks.rollup.edit.call_args.kwargs
        assert rollup_kwargs["status"] == "completed"
        assert rollup_kwargs["details_url"] == details(to_ts=1420000)
        assert logs(to_ts=1420000) in rollup_kwargs["output"]["summary"]

    def test_unconcluded_definition_checks_skipped_at_teardown(self):
        clock = FakeClock()
        handler = links_handler(clock=clock)
        handler._plan_checks.checks["never_ran"] = mock.Mock()
        handler._report.ensure("never_ran")
        execute(handler, PLAN, POST, NO_CHANGES)
        clock.now = 1060.0

        handler.teardown("dep", "/tmp")

        kwargs = handler._plan_checks.checks["never_ran"].edit.call_args.kwargs
        assert kwargs["conclusion"] == "skipped"
        assert kwargs["details_url"] == details("never_ran", to_ts=1360000)
        # a skipped check's summary is just the link
        assert kwargs["output"]["summary"] == logs("never_ran", to_ts=1360000)
        # already-concluded checks are not edited again
        handler._plan_checks.checks["mydef"].edit.assert_called_once()

    def test_apply_checks_created_and_concluded(self):
        clock = FakeClock()
        handler = links_handler(clock=clock)
        rollup, per_def = apply_checks(handler)

        execute(handler, APPLY, PRE)
        created = handler._repo.create_check_run.call_args_list
        assert created[0].kwargs["details_url"] == details()
        assert created[1].kwargs["details_url"] == details(definition="mydef")
        assert logs() in created[0].kwargs["output"]["summary"]
        assert "output" not in created[1].kwargs
        assert logs() in rollup.edit.call_args.kwargs["output"]["summary"]

        clock.now = 1060.0
        execute(handler, APPLY, POST, TerraformResult(0, APPLY_STDOUT, b""))
        concluded = per_def.edit.call_args.kwargs
        assert concluded["details_url"] == details("mydef", to_ts=1360000)
        assert concluded["output"]["summary"].startswith(
            f"{logs('mydef', to_ts=1360000)} · "
            "[View dep apply check run](https://github.test/apply)\n\n```\n"
        )

        clock.now = 1120.0
        handler.teardown("dep", "/tmp")
        kwargs = rollup.edit.call_args.kwargs
        assert kwargs["status"] == "completed"
        assert kwargs["details_url"] == details(to_ts=1420000)
        assert logs(to_ts=1420000) in kwargs["output"]["summary"]

    def test_no_templates_link_nothing(self):
        handler = make_handler()
        handler._app_state.root_options.run_id = "run-1234"
        run_setup(handler)
        execute(handler, PLAN, POST, NO_CHANGES)
        handler.teardown("dep", "/tmp")
        calls = handler._repo.create_check_run.call_args_list + [
            *handler._repo.create_check_run.return_value.edit.call_args_list
        ]
        assert calls
        assert all("details_url" not in c.kwargs for c in calls)
        assert not any("View logs" in str(c.kwargs.get("output", "")) for c in calls)

    def test_status_comment_has_no_logs_link(self):
        handler = links_handler()
        handler._update_comments()
        assert "View logs" not in handler._issue.create_comment.call_args.args[0]


def rendered_link(handler, kind, definition=None):
    """The details url or logs link url a check gets; None when unlinked."""
    if kind == "details":
        return handler._details_url(definition).get("details_url")
    # stripping the configured label checks it is the one rendered
    link = handler._logs_link(definition)
    label = f"[{handler.config.logs_label}]("
    return link.removeprefix(label).removesuffix(")") or None


def template_handler(kind, url=None, definition_url=None):
    """A handler with only the templates of one link kind set."""
    return links_handler(
        **{
            "details_url": None,
            "logs_url": None,
            f"{kind}_url": url,
            f"definition_{kind}_url": definition_url,
        }
    )


@pytest.mark.parametrize("kind", ["details", "logs"])
class TestLinkTemplates:
    def test_definition_template_preferred_for_definition_checks(self, kind):
        handler = template_handler(
            kind, LINK_TEMPLATE, "https://logs.example.com/def/{definition}"
        )
        assert rendered_link(handler, kind) == details()
        assert rendered_link(handler, kind, "mydef") == (
            "https://logs.example.com/def/mydef"
        )

    def test_definition_template_alone_leaves_rollups_unlinked(self, kind):
        handler = template_handler(
            kind, definition_url="https://logs.example.com/def/{definition}"
        )
        assert rendered_link(handler, kind) is None
        assert rendered_link(handler, kind, "mydef") == (
            "https://logs.example.com/def/mydef"
        )

    def test_env_templates_and_label_are_applied(self, kind, monkeypatch):
        monkeypatch.setenv(
            f"GITHUB_CHECK_{kind.upper()}_URL", "https://logs.example.com/r"
        )
        monkeypatch.setenv(
            f"GITHUB_CHECK_DEFINITION_{kind.upper()}_URL",
            "https://logs.example.com/d/{definition}",
        )
        monkeypatch.setenv("GITHUB_CHECK_LOGS_LABEL", "Logs")
        handler = make_handler()
        assert handler.config.logs_label == "Logs"
        assert rendered_link(handler, kind) == "https://logs.example.com/r"
        assert (
            rendered_link(handler, kind, "mydef") == "https://logs.example.com/d/mydef"
        )

    def test_values_are_url_quoted(self, kind):
        handler = template_handler(
            kind, "https://logs.example.com/?r={run_id}&d={deployment}&n={definition}"
        )
        handler._app_state.root_options.run_id = "run 1/2"
        handler._deployment = "a&b"
        assert rendered_link(handler, kind, "x y#z") == (
            "https://logs.example.com/?r=run%201%2F2&d=a%26b&n=x%20y%23z"
        )

    @pytest.mark.parametrize(
        "template",
        [
            "https://logs.example.com/{unknown}",
            "https://logs.example.com/{0}",
            "https://logs.example.com/{from_ts:s}",
            "https://logs.example.com/{run_id.x}",
            "https://logs.example.com/{",
        ],
    )
    def test_bad_template_warns_once_and_is_omitted(self, kind, template):
        handler = template_handler(kind, template)
        with mock.patch("tfworker.handlers.github.log.warn") as warn:
            run_setup(handler)
            execute(handler, PLAN, POST, NO_CHANGES)
            handler.teardown("dep", "/tmp")
        assert handler.is_ready() is True
        created = handler._repo.create_check_run.call_args_list
        edits = handler._repo.create_check_run.return_value.edit.call_args_list
        assert len(created) == 2
        assert edits
        for call in created + edits:
            assert "details_url" not in call.kwargs
            assert "View logs" not in str(call.kwargs.get("output"))
        assert warn.call_count == 1
        assert f"{kind} url template" in warn.call_args.args[0]


class TestRollupBackLinks:
    """Per-definition check runs link back to their rollup check run."""

    def test_definition_checks_link_to_the_created_rollup(self):
        handler = make_handler()
        rollup = mock.Mock(html_url="https://github.test/plan")
        per_def = mock.Mock(html_url="https://github.test/plan/mydef")
        handler._repo.create_check_run.side_effect = [rollup, per_def]
        run_setup(handler)
        assert handler._report.check_url == "https://github.test/plan"
        assert handler._report.rows["mydef"].url == "https://github.test/plan/mydef"

        handler.teardown("dep", "/tmp")

        output = per_def.edit.call_args.kwargs["output"]
        assert (
            output["summary"] == "[View dep plan check run](https://github.test/plan)"
        )
        assert (
            "https://github.test/plan)"
            not in rollup.edit.call_args.kwargs["output"]["summary"]
        )

    def test_omitted_when_the_rollup_is_unknown(self):
        handler = make_handler()
        execute(handler, PLAN, ERROR, TerraformResult(1, b"", b"Error: x\n"))
        output = handler._plan_checks.checks["mydef"].edit.call_args.kwargs["output"]
        assert output["summary"] == "```\nError: x\n```"
