from unittest import mock

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from tfworker_console import version
from tfworker_console.app import create_app
from tfworker_console.cli import main
from tfworker_console.settings import ConsoleSettings


def test_livez():
    response = TestClient(create_app()).get("/livez")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == version()
    assert "tfworker.plan/v1" in body["schemas"]


def test_settings_from_environment(monkeypatch):
    monkeypatch.setenv("TFWORKER_CONSOLE_PORT", "9090")
    monkeypatch.setenv("TFWORKER_CONSOLE_DATABASE_URL", "postgresql://db/console")
    settings = ConsoleSettings()
    assert settings.port == 9090
    assert settings.database_url == "postgresql://db/console"
    assert create_app(settings).state.settings is settings


def test_serve_uses_settings(monkeypatch):
    monkeypatch.setenv("TFWORKER_CONSOLE_HOST", "0.0.0.0")
    # serve writes its options to the environment; setting them here first
    # makes monkeypatch restore them afterwards
    monkeypatch.setenv("TFWORKER_CONSOLE_PORT", "8000")
    with mock.patch("tfworker_console.cli.uvicorn.run") as run:
        result = CliRunner().invoke(main, ["serve", "--port", "8081"])
    assert result.exit_code == 0, result.output
    run.assert_called_once_with(
        "tfworker_console.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8081,
        reload=False,
    )


def test_serve_options_reach_the_app(monkeypatch):
    # dev auth judges the host the app will really listen on
    monkeypatch.setenv("TFWORKER_CONSOLE_AUTH", "none")
    monkeypatch.setenv("TFWORKER_CONSOLE_HOST", "127.0.0.1")
    monkeypatch.setenv("TFWORKER_CONSOLE_PORT", "8000")
    with mock.patch("tfworker_console.cli.uvicorn.run"):
        result = CliRunner().invoke(main, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 0, result.output
    with pytest.raises(ValueError, match="only listens on loopback"):
        create_app()


def test_version_option():
    from click.testing import CliRunner

    from tfworker_console import cli, version

    result = CliRunner().invoke(cli.main, ["--version"])
    assert result.exit_code == 0
    assert version() in result.output
