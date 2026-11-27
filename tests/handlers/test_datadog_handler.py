import os
from unittest.mock import MagicMock, patch

import pytest

from tfworker.commands.terraform import TerraformResult
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.definitions.model import Definition

# Both credentials are required by the config validator. Tests that don't care
# about credential resolution provide raw values for both.
_RAW_KEYS = {"api_key": "placeholder-api-key", "app_key": "placeholder-app-key"}


class TestDatadogConfig:
    def test_api_key_raw_value(self):
        from tfworker.handlers.datadog import DatadogConfig

        cfg = DatadogConfig(api_key="dd-raw", app_key="app-raw")
        assert cfg.resolved_api_key == "dd-raw"

    def test_app_key_raw_value(self):
        from tfworker.handlers.datadog import DatadogConfig

        cfg = DatadogConfig(api_key="dd-raw", app_key="app-raw")
        assert cfg.resolved_app_key == "app-raw"

    def test_keys_from_default_env_vars(self):
        from tfworker.handlers.datadog import DatadogConfig

        with patch.dict(
            os.environ,
            {"DD_API_KEY": "dd-from-env", "DD_APP_KEY": "app-from-env"},
            clear=True,
        ):
            cfg = DatadogConfig()
            assert cfg.resolved_api_key == "dd-from-env"
            assert cfg.resolved_app_key == "app-from-env"

    def test_keys_from_custom_env_vars(self):
        from tfworker.handlers.datadog import DatadogConfig

        with patch.dict(
            os.environ,
            {"MY_DD_KEY": "dd-custom", "MY_DD_APP_KEY": "app-custom"},
            clear=True,
        ):
            cfg = DatadogConfig(api_key_env="MY_DD_KEY", app_key_env="MY_DD_APP_KEY")
            assert cfg.resolved_api_key == "dd-custom"
            assert cfg.resolved_app_key == "app-custom"

    def test_raw_key_takes_precedence_over_env(self):
        from tfworker.handlers.datadog import DatadogConfig

        with patch.dict(
            os.environ,
            {"DD_API_KEY": "dd-env", "DD_APP_KEY": "app-env"},
            clear=True,
        ):
            cfg = DatadogConfig(api_key="dd-raw", app_key="app-raw")
            assert cfg.resolved_api_key == "dd-raw"
            assert cfg.resolved_app_key == "app-raw"

    def test_missing_api_key_raises(self):
        from tfworker.handlers.datadog import DatadogConfig

        # Only an app key is available; the api key cannot be resolved.
        with patch.dict(os.environ, {"DD_APP_KEY": "app-env"}, clear=True):
            with pytest.raises(ValueError):
                DatadogConfig()

    def test_missing_app_key_raises(self):
        from tfworker.handlers.datadog import DatadogConfig

        # Only an api key is available; the app key cannot be resolved.
        with patch.dict(os.environ, {"DD_API_KEY": "dd-env"}, clear=True):
            with pytest.raises(ValueError):
                DatadogConfig()

    def test_keys_not_exposed_in_repr(self):
        from tfworker.handlers.datadog import DatadogConfig

        cfg = DatadogConfig(api_key="dd-super-secret", app_key="app-super-secret")
        assert "dd-super-secret" not in repr(cfg)
        assert "app-super-secret" not in repr(cfg)

    def test_defaults(self):
        from tfworker.handlers.datadog import DatadogConfig

        cfg = DatadogConfig(**_RAW_KEYS)
        assert cfg.api_key_env == "DD_API_KEY"
        assert cfg.app_key_env == "DD_APP_KEY"

    def test_events_url(self):
        from tfworker.handlers.datadog import DatadogConfig

        cfg = DatadogConfig(**_RAW_KEYS)
        assert cfg.events_url == (
            "https://event-management-intake.datadoghq.com/api/v2/events"
        )


class TestDatadogGitInfo:
    def _make_handler(self):
        from tfworker.handlers.datadog import DatadogConfig, DatadogHandler

        return DatadogHandler(DatadogConfig(**_RAW_KEYS))

    def test_git_fields_collected_from_cli(self):
        h = self._make_handler()
        # _resolve_git_info calls _git first for the branch (rev-parse), then for
        # each of commit, short_commit, author_name, author_email, subject (in
        # order, via git log --format).
        outputs = ["br", "fullsha", "shortsha", "Jane Doe", "jane@x.com", "fix it"]
        with patch.object(h, "_git", side_effect=outputs):
            info = h._resolve_git_info("/somedir")
        assert info["branch"] == "br"
        assert info["commit"] == "fullsha"
        assert info["short_commit"] == "shortsha"
        assert info["author_name"] == "Jane Doe"
        assert info["author_email"] == "jane@x.com"
        assert info["subject"] == "fix it"

    def test_git_info_defaults_to_empty_strings(self):
        h = self._make_handler()
        # When every git invocation fails, all fields fall back to "" so a
        # missing git context never aborts the run.
        with patch.object(h, "_git", return_value=""):
            info = h._resolve_git_info("/somedir")
        assert info == {
            "branch": "",
            "commit": "",
            "short_commit": "",
            "author_name": "",
            "author_email": "",
            "subject": "",
        }

    def test_git_returns_empty_on_failure(self):
        from tfworker.handlers.datadog import DatadogHandler

        with patch("subprocess.check_output", side_effect=Exception("no git")):
            assert DatadogHandler._git("/tmp", "log") == ""


class TestDatadogBuildPayload:
    def _make_handler(self):
        from tfworker.handlers.datadog import DatadogConfig, DatadogHandler

        return DatadogHandler(DatadogConfig(**_RAW_KEYS))

    def _defn(self, name="vpc"):
        return Definition(name=name, path="/tmp")

    def _result(self):
        return TerraformResult(0, b"ok", b"")

    def _git_info(self, **overrides):
        info = {
            "branch": "main",
            "commit": "abc123def456",
            "short_commit": "abc123d",
            "author_name": "Jane Doe",
            "author_email": "jane@x.com",
            "subject": "fix vpc",
        }
        info.update(overrides)
        return info

    def _attrs(self, payload):
        """The inner attributes block holding the change-event details."""
        return payload["data"]["attributes"]["attributes"]

    def test_category_is_change(self):
        h = self._make_handler()
        p = h._build_payload(
            TerraformAction.APPLY,
            self._defn(),
            "prod-use1",
            self._result(),
            self._git_info(),
        )
        assert p["data"]["attributes"]["category"] == "change"

    def test_type_is_event(self):
        h = self._make_handler()
        p = h._build_payload(
            TerraformAction.APPLY,
            self._defn(),
            "prod-use1",
            self._result(),
            self._git_info(),
        )
        assert p["data"]["type"] == "event"

    def test_title_includes_action_deployment_and_definition(self):
        h = self._make_handler()
        p = h._build_payload(
            TerraformAction.APPLY,
            self._defn(),
            "prod-use1",
            self._result(),
            self._git_info(),
        )
        assert p["data"]["attributes"]["title"] == "Terraform apply of prod-use1/vpc"

    def test_author_uses_email(self):
        h = self._make_handler()
        p = h._build_payload(
            TerraformAction.APPLY,
            self._defn(),
            "prod-use1",
            self._result(),
            self._git_info(),
        )
        assert self._attrs(p)["author"] == {"type": "user", "name": "jane@x.com"}

    def test_author_falls_back_to_name_then_unknown(self):
        h = self._make_handler()
        p = h._build_payload(
            TerraformAction.APPLY,
            self._defn(),
            "prod-use1",
            self._result(),
            self._git_info(author_email="", author_name="Jane Doe"),
        )
        assert self._attrs(p)["author"]["name"] == "Jane Doe"

        p2 = h._build_payload(
            TerraformAction.APPLY,
            self._defn(),
            "prod-use1",
            self._result(),
            self._git_info(author_email="", author_name=""),
        )
        assert self._attrs(p2)["author"]["name"] == "unknown"

    def test_changed_resource_is_configuration_named_after_definition(self):
        h = self._make_handler()
        p = h._build_payload(
            TerraformAction.APPLY,
            self._defn(),
            "prod-use1",
            self._result(),
            self._git_info(),
        )
        cr = self._attrs(p)["changed_resource"]
        assert cr == {"type": "configuration", "name": "vpc"}

    def test_impacted_resource_name_uses_hyphens(self):
        h = self._make_handler()
        p = h._build_payload(
            TerraformAction.APPLY,
            self._defn("my_service"),
            "prod-use1",
            self._result(),
            self._git_info(),
        )
        impacted = self._attrs(p)["impacted_resources"]
        assert impacted == [{"type": "service", "name": "my-service"}]

    def test_tags_include_context(self):
        h = self._make_handler()
        p = h._build_payload(
            TerraformAction.DESTROY,
            self._defn(),
            "prod-use1",
            self._result(),
            self._git_info(),
        )
        tags = p["data"]["attributes"]["tags"]
        assert "env:use1" in tags
        assert "definition:vpc" in tags
        assert "service:vpc" in tags
        assert "git_branch:main" in tags
        assert "git_commit:abc123d" in tags

    def test_tags_omit_git_context_when_absent(self):
        h = self._make_handler()
        p = h._build_payload(
            TerraformAction.APPLY,
            self._defn(),
            "prod-use1",
            self._result(),
            self._git_info(branch="", short_commit=""),
        )
        tags = p["data"]["attributes"]["tags"]
        assert not any(t.startswith("git_branch:") for t in tags)
        assert not any(t.startswith("git_commit:") for t in tags)

    def test_change_metadata_omits_empty_values(self):
        h = self._make_handler()
        p = h._build_payload(
            TerraformAction.APPLY,
            self._defn(),
            "prod-use1",
            self._result(),
            self._git_info(branch="", subject=""),
        )
        meta = self._attrs(p)["change_metadata"]
        assert "branch" not in meta
        assert "commit_subject" not in meta
        assert meta["commit_sha"] == "abc123def456"
        assert meta["commit_short_sha"] == "abc123d"
        assert meta["category"] == "prod"
        assert meta["env"] == "use1"
        assert meta["action"] == "apply"

    def test_change_metadata_includes_definition_dump(self):
        h = self._make_handler()
        p = h._build_payload(
            TerraformAction.APPLY,
            self._defn(),
            "prod-use1",
            self._result(),
            self._git_info(),
        )
        meta = self._attrs(p)["change_metadata"]
        assert meta["definition"]["name"] == "vpc"


class TestDatadogExecute:
    def _make_handler(self):
        from tfworker.handlers.datadog import DatadogConfig, DatadogHandler

        h = DatadogHandler(DatadogConfig(**_RAW_KEYS))
        h._http = MagicMock()
        h._http.request.return_value = MagicMock(status=202, data=b"{}")
        return h

    def _defn(self, name="vpc"):
        return Definition(name=name, path="/tmp")

    def test_apply_success_posts_event(self):
        h = self._make_handler()
        with patch.object(h, "_resolve_git_info", return_value={}):
            with patch.object(h, "_build_payload", return_value={"data": {}}):
                h.execute(
                    TerraformAction.APPLY,
                    TerraformStage.POST,
                    "prod-use1",
                    self._defn(),
                    "/tmp",
                    TerraformResult(0, b"ok", b""),
                )
        h._http.request.assert_called_once()

    def test_destroy_success_posts_event(self):
        h = self._make_handler()
        with patch.object(h, "_resolve_git_info", return_value={}):
            with patch.object(h, "_build_payload", return_value={"data": {}}):
                h.execute(
                    TerraformAction.DESTROY,
                    TerraformStage.POST,
                    "prod-use1",
                    self._defn(),
                    "/tmp",
                    TerraformResult(0, b"ok", b""),
                )
        h._http.request.assert_called_once()

    def test_plan_does_not_post(self):
        h = self._make_handler()
        h.execute(
            TerraformAction.PLAN,
            TerraformStage.POST,
            "prod-use1",
            self._defn(),
            "/tmp",
            TerraformResult(0, b"ok", b""),
        )
        h._http.request.assert_not_called()

    def test_pre_stage_does_not_post(self):
        h = self._make_handler()
        h.execute(
            TerraformAction.APPLY,
            TerraformStage.PRE,
            "prod-use1",
            self._defn(),
            "/tmp",
            None,
        )
        h._http.request.assert_not_called()

    def test_failed_apply_does_not_post(self):
        h = self._make_handler()
        h.execute(
            TerraformAction.APPLY,
            TerraformStage.POST,
            "prod-use1",
            self._defn(),
            "/tmp",
            TerraformResult(1, b"", b"boom"),
        )
        h._http.request.assert_not_called()

    def test_none_result_does_not_post(self):
        h = self._make_handler()
        h.execute(
            TerraformAction.APPLY,
            TerraformStage.POST,
            "prod-use1",
            self._defn(),
            "/tmp",
            None,
        )
        h._http.request.assert_not_called()

    def test_post_sends_credential_headers_and_json_body(self):
        h = self._make_handler()
        with patch.object(h, "_resolve_git_info", return_value={}):
            with patch.object(h, "_build_payload", return_value={"data": {"x": 1}}):
                h.execute(
                    TerraformAction.APPLY,
                    TerraformStage.POST,
                    "prod-use1",
                    self._defn(),
                    "/tmp",
                    TerraformResult(0, b"ok", b""),
                )
        args, kwargs = h._http.request.call_args
        assert args[0] == "POST"
        assert args[1].endswith("/api/v2/events")
        assert kwargs["headers"]["DD-API-KEY"] == "placeholder-api-key"
        assert kwargs["headers"]["DD-APP-KEY"] == "placeholder-app-key"
        assert kwargs["json"] == {"data": {"x": 1}}


class TestDatadogPostErrors:
    """The handler no longer raises on post failures; it warns and continues so
    a Datadog outage never aborts a terraform run."""

    def _make_handler(self):
        from tfworker.handlers.datadog import DatadogConfig, DatadogHandler

        h = DatadogHandler(DatadogConfig(**_RAW_KEYS))
        h._http = MagicMock()
        return h

    def test_non_2xx_warns_and_does_not_raise(self):
        h = self._make_handler()
        h._http.request.return_value = MagicMock(status=403, data=b"forbidden")
        with patch("tfworker.handlers.datadog.log.warn") as warn:
            h._post_event({"data": {}})
        warn.assert_called_once()
        assert "403" in warn.call_args.args[0]

    def test_request_exception_warns_and_does_not_raise(self):
        h = self._make_handler()
        h._http.request.side_effect = Exception("network down")
        with patch("tfworker.handlers.datadog.log.warn") as warn:
            h._post_event({"data": {}})
        warn.assert_called_once()
        assert "network down" in warn.call_args.args[0]

    def test_2xx_does_not_warn(self):
        h = self._make_handler()
        h._http.request.return_value = MagicMock(status=202, data=b"{}")
        with patch("tfworker.handlers.datadog.log.warn") as warn:
            h._post_event({"data": {}})
        warn.assert_not_called()


class TestDatadogIsReady:
    """is_ready() validates the API key against Datadog once, then caches the
    result. Any failure warns and reports not-ready so a Datadog problem never
    aborts a terraform run."""

    def _make_handler(self):
        from tfworker.handlers.datadog import DatadogConfig, DatadogHandler

        h = DatadogHandler(DatadogConfig(**_RAW_KEYS))
        h._http = MagicMock()
        return h

    def test_valid_credentials_ready(self):
        h = self._make_handler()
        h._http.request.return_value = MagicMock(status=200, data=b'{"valid": true}')
        assert h.is_ready() is True

    def test_validate_uses_api_key_header_and_url(self):
        h = self._make_handler()
        h._http.request.return_value = MagicMock(status=200, data=b'{"valid": true}')
        h.is_ready()
        args, kwargs = h._http.request.call_args
        assert args[0] == "GET"
        assert args[1].endswith("/api/v1/validate")
        assert kwargs["headers"]["DD-API-KEY"] == "placeholder-api-key"

    def test_invalid_flag_not_ready_and_warns(self):
        h = self._make_handler()
        h._http.request.return_value = MagicMock(status=200, data=b'{"valid": false}')
        with patch("tfworker.handlers.datadog.log.warn") as warn:
            assert h.is_ready() is False
        warn.assert_called_once()

    def test_non_200_not_ready_and_warns(self):
        h = self._make_handler()
        h._http.request.return_value = MagicMock(status=403, data=b"Forbidden")
        with patch("tfworker.handlers.datadog.log.warn") as warn:
            assert h.is_ready() is False
        warn.assert_called_once()
        assert "403" in warn.call_args.args[0]

    def test_request_exception_not_ready_and_warns(self):
        h = self._make_handler()
        h._http.request.side_effect = Exception("network down")
        with patch("tfworker.handlers.datadog.log.warn") as warn:
            assert h.is_ready() is False
        warn.assert_called_once()
        assert "network down" in warn.call_args.args[0]

    def test_success_result_is_cached(self):
        h = self._make_handler()
        h._http.request.return_value = MagicMock(status=200, data=b'{"valid": true}')
        assert h.is_ready() is True
        assert h.is_ready() is True
        h._http.request.assert_called_once()

    def test_failure_result_is_cached(self):
        h = self._make_handler()
        h._http.request.return_value = MagicMock(status=403, data=b"Forbidden")
        assert h.is_ready() is False
        assert h.is_ready() is False
        h._http.request.assert_called_once()


class TestDatadogRegistry:
    def test_registered_as_datadog(self):
        import tfworker.handlers.datadog  # noqa: F401
        from tfworker.handlers.datadog import DatadogHandler
        from tfworker.handlers.registry import HandlerRegistry

        assert HandlerRegistry.get_handler("datadog") is DatadogHandler

    def test_config_model_is_datadog_config(self):
        import tfworker.handlers.datadog  # noqa: F401
        from tfworker.handlers.datadog import DatadogConfig
        from tfworker.handlers.registry import HandlerRegistry

        assert HandlerRegistry.get_handler_config_model("datadog") is DatadogConfig

    def test_actions_are_apply_and_destroy(self):
        from tfworker.handlers.datadog import DatadogHandler

        assert TerraformAction.APPLY in DatadogHandler.actions
        assert TerraformAction.DESTROY in DatadogHandler.actions
        assert TerraformAction.PLAN not in DatadogHandler.actions
