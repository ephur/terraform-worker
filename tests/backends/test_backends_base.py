import json

import pytest

from tfworker.backends.base import validate_backend_empty
from tfworker.exceptions import BackendError


class TestBalidateBackendEmpty:

    def test_validate_backend_empty(self, empty_state):
        assert validate_backend_empty(json.loads(empty_state)) is True

    def test_validate_backend_empty_false(self, occupied_state):
        assert validate_backend_empty(json.loads(occupied_state)) is False

    def test_validate_backend_missing_key(self):
        state = json.loads("{}")
        with pytest.raises(BackendError, match="key does not exist"):
            validate_backend_empty(state)

    def test_validate_backend_invalid_type(self):
        state = "bad state"
        with pytest.raises(BackendError, match="not valid JSON"):
            validate_backend_empty(state)


class TestLockInspectionDefaults:
    def test_base_backend_lock_methods_not_implemented(self):
        from tfworker.backends.base import BaseBackend

        class Minimal(BaseBackend):
            tag = "minimal"
            auth_tag = "none"

            def __init__(self):
                pass

            def hcl(self, name):
                return ""

            def data_hcl(self, remotes):
                return ""

            def clean(self, deployment, limit=None):
                pass

            @property
            def remotes(self):
                return []

            def get_state(self, remote):
                return {}

        backend = Minimal()
        assert backend.lock_inspection is False
        for call in (
            lambda: backend.get_lock("d"),
            lambda: backend.get_lease("d", "h"),
            lambda: backend.put_lease(None),
            lambda: backend.delete_lease("d", "h"),
        ):
            with pytest.raises(NotImplementedError):
                call()

    def test_base_backend_report_methods_not_implemented(self):
        from tfworker.backends.base import BaseBackend

        assert BaseBackend.report_storage is False
        for name, args in (
            ("location", ("d",)),
            ("state_version", ("d",)),
            ("read_report", ("p",)),
            ("write_report", ("p", "{}")),
        ):
            with pytest.raises(NotImplementedError):
                getattr(BaseBackend, name)(None, *args)
