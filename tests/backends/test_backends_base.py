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
