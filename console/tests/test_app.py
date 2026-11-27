from unittest import mock

from click.testing import CliRunner
from fastapi.testclient import TestClient

from tfworker_console import version
from tfworker_console.app import create_app
from tfworker_console.cli import main
from tfworker_console.settings import ConsoleSettings


def test_healthz():
    response = TestClient(create_app()).get("/healthz")
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
