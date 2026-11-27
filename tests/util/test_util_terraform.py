"""Tests for tfworker.util.terraform's subprocess-driving functions."""

from contextlib import contextmanager

import pytest

import tfworker.util.terraform as tf_util
from tfworker.exceptions import TFWorkerException


@contextmanager
def fake_mirror_configuration(*args, **kwargs):
    yield "/tmp/mirror"


class TestMirrorProviders:
    def test_mirror_delegates_to_pipe_exec_logged(self, mocker):
        mocker.patch.object(
            tf_util.tfhelpers,
            "_write_mirror_configuration",
            side_effect=fake_mirror_configuration,
        )
        run = mocker.patch(
            "tfworker.util.terraform.pipe_exec_logged", return_value=(0, b"", b"")
        )

        tf_util.mirror_providers("providers", "/bin/terraform", "/working", "/cache")

        kwargs = run.call_args.kwargs
        assert kwargs["label"] == "terraform providers mirror"
        assert kwargs["cwd"] == "/tmp/mirror"
        assert kwargs["extra"] == {"cache_dir": "/cache"}
        assert run.call_args.args[0] == "/bin/terraform providers mirror /cache"

    def test_mirror_raises_on_failure(self, mocker):
        mocker.patch.object(
            tf_util.tfhelpers,
            "_write_mirror_configuration",
            side_effect=fake_mirror_configuration,
        )
        mocker.patch(
            "tfworker.util.terraform.pipe_exec_logged",
            return_value=(1, b"", b"no such provider"),
        )

        with pytest.raises(TFWorkerException, match="Unable to mirror providers"):
            tf_util.mirror_providers(
                "providers", "/bin/terraform", "/working", "/cache"
            )

    def test_mirror_skips_when_all_cached(self, mocker):
        """An IndexError from the config writer means nothing needs mirroring."""
        mocker.patch.object(
            tf_util.tfhelpers, "_write_mirror_configuration", side_effect=IndexError
        )
        run = mocker.patch("tfworker.util.terraform.pipe_exec_logged")

        tf_util.mirror_providers("providers", "/bin/terraform", "/working", "/cache")

        run.assert_not_called()
