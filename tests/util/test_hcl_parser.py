import json
import stat
from pathlib import Path

import pytest

from tfworker.util import hcl_parser


class TestHCLParser:
    def test_parse_string_python_engine(self, monkeypatch):
        # Force python engine
        monkeypatch.setenv("TFWORKER_HCL_ENGINE", "python")
        hcl = """
        terraform {
          worker_options { x = 1 }
        }
        """
        d = hcl_parser.parse_string(hcl)
        assert "terraform" in d

    def test_parse_with_go_binary(self, tmp_path: Path, monkeypatch):
        # Create a fake go helper that just prints a known JSON
        payload = {"terraform": [{"worker_options": [{"x": 1}]}]}
        fake = tmp_path / "tfworker-hcl2json"
        fake.write_text("#!/bin/sh\n" + f"echo '{json.dumps(payload)}'\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

        monkeypatch.setenv("TFWORKER_HCL_ENGINE", "go")
        monkeypatch.setenv("TFWORKER_HCL_BIN", str(fake))

        d = hcl_parser.parse_string("terraform {}")
        assert d == payload

    def test_go_binary_error_surface(self, tmp_path: Path, monkeypatch):
        # Fake helper exits non-zero
        fake = tmp_path / "tfworker-hcl2json"
        fake.write_text("#!/bin/sh\nexit 3\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

        monkeypatch.setenv("TFWORKER_HCL_ENGINE", "go")
        monkeypatch.setenv("TFWORKER_HCL_BIN", str(fake))

        with pytest.raises(ValueError):
            hcl_parser.parse_string("terraform {}")

    def test_parse_files_multi_go_and_errors(self, tmp_path: Path, monkeypatch):
        # Create two files: one valid, one invalid
        good = tmp_path / "good.hcl"
        bad = tmp_path / "bad.hcl"
        good.write_text("terraform { required_providers {} }")
        bad.write_text("terraform { izgreat! }")

        # Fake helper: returns ok for good and error for bad
        payload = {
            "ok": {str(good): {"terraform": [{"required_providers": []}]}},
            "errors": {str(bad): "parse error"},
        }
        fake = tmp_path / "tfworker-hcl2json"
        fake.write_text("#!/bin/sh\n" + f"echo '{json.dumps(payload)}'\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

        monkeypatch.setenv("TFWORKER_HCL_ENGINE", "go")
        monkeypatch.setenv("TFWORKER_HCL_BIN", str(fake))

        ok, errors = hcl_parser.parse_files([str(good), str(bad)])
        assert str(good) in ok
        assert str(bad) in errors
