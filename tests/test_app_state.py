import pytest

from tfworker.app_state import AppState
from tfworker.cli_options import CLIOptionsTerraform
from tfworker.exceptions import FrozenInstanceError


class TestAppState:
    def test_app_state_model(self):
        app_state = AppState()
        assert app_state.deployment == "undefined"
        assert app_state.model_config == {
            "extra": "forbid",
            "arbitrary_types_allowed": True,
        }
        assert app_state.authenticators is None
        assert app_state.backend is None
        assert app_state.clean_options is None
        assert app_state.definitions is None
        assert app_state.handlers is None
        assert app_state.loaded_config == {}
        assert app_state.providers is None

    def test_app_state_freeze(self):
        app_state = AppState()
        app_state.terraform_options = CLIOptionsTerraform()
        app_state.freeze()

        # ensure primary model is frozen
        with pytest.raises(FrozenInstanceError):
            app_state.deployment = "test"

        # ensure nested models are frozen
        with pytest.raises(FrozenInstanceError):
            app_state.terraform_options.apply = True
