import pytest


@pytest.fixture(autouse=True)
def _auth_off_by_default(monkeypatch):
    """Views are tested without sign-in; test_auth.py turns it on explicitly."""
    monkeypatch.setenv("TFWORKER_CONSOLE_AUTH", "none")
