from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Tuple


def _engine() -> str:
    """
    Determine which HCL parsing engine to use.

    Selection logic:
    - If TFWORKER_HCL_ENGINE is set to 'go' or 'python', force that engine.
      - If 'go' is forced but the helper binary is missing, raise ValueError.
    - If TFWORKER_HCL_ENGINE is unset or set to 'auto', auto-detect:
      - Use 'go' if the helper binary is available (via TFWORKER_HCL_BIN or PATH),
        otherwise use 'python'.
    """
    prefer = os.getenv("TFWORKER_HCL_ENGINE", "").strip().lower()

    if prefer in ("go", "python"):
        if prefer == "go":
            if _go_binary_path() is None:
                raise ValueError(
                    "TFWORKER_HCL_ENGINE=go but Go HCL helper binary not found; "
                    "ensure it's on PATH or set TFWORKER_HCL_BIN"
                )
        return prefer

    # Auto-detect ('auto' or unset)
    if _go_binary_path() is not None:
        return "go"
    return "python"


def _go_binary_path() -> str | None:
    """
    Resolve the path to the Go-backed HCL helper binary.
    Looks for TFWORKER_HCL_BIN or a binary named 'tfworker-hcl2json' on PATH.
    """
    explicit = os.getenv("TFWORKER_HCL_BIN")
    if explicit:
        return explicit
    return shutil.which("tfworker-hcl2json")


def parse_string(rendered: str) -> Dict[str, Any]:
    """Parse HCL from a string into a Python dict.

    Attempts to use the Go-backed parser if configured; otherwise falls back to
    python-hcl2 for broad compatibility.
    """
    if _engine() == "go":
        payload = _invoke_go(["--stdin"], stdin=rendered)
        if not isinstance(payload, dict):
            raise ValueError("unexpected response from Go HCL helper for string input")
        return payload

    import hcl2  # type: ignore

    return hcl2.loads(rendered)


def parse_file(path: str) -> Dict[str, Any]:
    """Parse HCL from a file into a Python dict."""
    if _engine() == "go":
        payload = _invoke_go([path])
        if not isinstance(payload, dict):
            raise ValueError("unexpected response from Go HCL helper for file input")
        return payload

    import hcl2  # type: ignore

    with open(path, "r") as f:
        return hcl2.load(f)


def parse_files(paths: List[str]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    """Parse multiple HCL files.

    Returns a tuple of (ok, errors):
    - ok: mapping of path -> parsed dict
    - errors: mapping of path -> error string

    Uses Go helper in multi-file mode when available; otherwise
    falls back to parsing each file with python-hcl2.
    """
    engine = _engine()
    if engine == "go":
        if len(paths) == 1:
            payload = _invoke_go([paths[0]])
            if not isinstance(payload, dict):
                raise ValueError(
                    "unexpected response from Go HCL helper for file input"
                )
            return {paths[0]: payload}, {}
        payload = _invoke_go(["--multi", *paths])
        if not isinstance(payload, dict):
            raise ValueError("invalid multi-file response structure from Go HCL helper")
        ok = payload.get("ok", {}) or {}
        errors = payload.get("errors", {}) or {}
        if not isinstance(ok, dict) or not isinstance(errors, dict):
            raise ValueError("invalid multi-file response structure from Go HCL helper")
        return ok, errors

    import hcl2  # type: ignore

    ok: Dict[str, Dict[str, Any]] = {}
    errors: Dict[str, str] = {}
    for p in paths:
        try:
            with open(p, "r") as f:
                ok[p] = hcl2.load(f)
        except Exception as e:  # mirror behavior: capture errors and continue
            errors[p] = str(e)
    return ok, errors


def _invoke_go(args: List[str], stdin: str | None = None) -> Any:
    """Invoke the Go helper with arbitrary args and optional stdin; return parsed JSON."""
    bin_path = _go_binary_path()
    if not bin_path:
        raise ValueError("Go HCL helper binary not found on PATH")

    cmd = [bin_path] + list(args)
    try:
        proc = subprocess.run(
            cmd,
            input=(stdin.encode() if stdin is not None else None),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as e:
        raise ValueError(f"failed to run Go HCL helper: {e}") from e

    if proc.returncode != 0:
        raise ValueError(
            f"Go HCL helper failed with code {proc.returncode}: {proc.stderr.decode().strip()}"
        )

    try:
        payload = json.loads(proc.stdout.decode())
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON from Go HCL helper: {e}") from e

    return payload
