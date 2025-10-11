import os
import shutil
from pathlib import Path

import pytest

from tfworker.util.terraform_helpers import _find_required_providers


def _helper_on_path() -> str | None:
    # Respect explicit env if provided
    explicit = os.getenv("TFWORKER_HCL_BIN")
    if explicit and Path(explicit).exists():
        return explicit
    # Check PATH
    found = shutil.which("tfworker-hcl2json")
    if found:
        return found
    # Check repo root build location (make go-build outputs here)
    repo_root = Path(__file__).parents[2]
    candidate = repo_root / "tfworker-hcl2json"
    if candidate.exists():
        return str(candidate)
    return None


@pytest.mark.integration
def test_required_providers_engine_parity(monkeypatch):
    """
    Ensure python and go engines return identical provider requirements
    for a known fixture directory.
    """
    fixtures_dir = Path(__file__).parents[1] / "fixtures" / "definitions" / "test_a"

    # Baseline using Python engine
    monkeypatch.setenv("TFWORKER_HCL_ENGINE", "python")
    providers_py = _find_required_providers(str(fixtures_dir))

    # If the Go helper isn't available, skip parity check gracefully
    helper = _helper_on_path()
    if not helper:
        pytest.skip("Go HCL helper not found; skipping engine parity test")

    # Compare with Go engine
    monkeypatch.setenv("TFWORKER_HCL_ENGINE", "go")
    monkeypatch.setenv("TFWORKER_HCL_BIN", helper)
    providers_go = _find_required_providers(str(fixtures_dir))

    assert providers_go == providers_py
