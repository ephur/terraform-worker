import os
from unittest.mock import patch

import pytest

from tfworker.exceptions import HandlerError


class TestSlackConfig:
    def test_requires_channel(self):
        """channel is required."""
        from tfworker.handlers.slack import SlackConfig
        with pytest.raises(Exception):
            SlackConfig()

    def test_token_raw_value(self):
        """token field is used directly when provided."""
        from tfworker.handlers.slack import SlackConfig
        cfg = SlackConfig(channel="#ops", token="xoxb-raw")
        assert cfg.resolved_token == "xoxb-raw"

    def test_token_from_default_env_var(self):
        """Falls back to SLACK_BOT_TOKEN env var."""
        from tfworker.handlers.slack import SlackConfig
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-from-env"}):
            cfg = SlackConfig(channel="#ops")
            assert cfg.resolved_token == "xoxb-from-env"

    def test_token_from_custom_env_var(self):
        """token_env overrides the default env var name."""
        from tfworker.handlers.slack import SlackConfig
        with patch.dict(os.environ, {"MY_TOKEN": "xoxb-custom"}, clear=False):
            cfg = SlackConfig(channel="#ops", token_env="MY_TOKEN")
            assert cfg.resolved_token == "xoxb-custom"

    def test_token_raw_takes_precedence_over_env(self):
        """Raw token takes precedence over env var."""
        from tfworker.handlers.slack import SlackConfig
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-env"}):
            cfg = SlackConfig(channel="#ops", token="xoxb-raw")
            assert cfg.resolved_token == "xoxb-raw"

    def test_missing_token_raises(self):
        """Raises ValueError when no token is resolvable."""
        from tfworker.handlers.slack import SlackConfig
        env = {k: v for k, v in os.environ.items() if k != "SLACK_BOT_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises((ValueError, Exception)):
                SlackConfig(channel="#ops")

    def test_defaults(self):
        """Default values are correct."""
        from tfworker.handlers.slack import SlackConfig
        cfg = SlackConfig(channel="#ops", token="xoxb-x")
        assert cfg.token_env == "SLACK_BOT_TOKEN"
        assert cfg.thread_reply is False
        assert cfg.title is None
        assert cfg.thread_reply_text is None
