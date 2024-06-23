from unittest.mock import patch

import pytest
from pydantic import ValidationError
from pydantic_core import InitErrorDetails

from tfworker.authenticators.base import BaseAuthenticator, BaseAuthenticatorConfig
from tfworker.authenticators.collection import AuthenticatorsCollection
from tfworker.exceptions import FrozenInstanceError, UnknownAuthenticator


class MockAuthenticatorConfig(BaseAuthenticatorConfig):
    pass


class MockAuthenticator(BaseAuthenticator):
    config_model = MockAuthenticatorConfig

    def __init__(self, auth_config: BaseAuthenticatorConfig):
        self.auth_config = auth_config

    def env(self):
        pass


MockAuthenticator.tag = "mock"


@pytest.fixture
def authenticators_collection(mock_cli_options_root):
    # Create a fresh instance of ALL for each test
    all_authenticators = [MockAuthenticator]
    with patch("tfworker.authenticators.collection.ALL", all_authenticators):
        AuthenticatorsCollection._instance = None
        return AuthenticatorsCollection(mock_cli_options_root)


class TestAuthenticatorsCollection:

    def test_singleton_behavior(self, mock_cli_options_root):
        instance1 = AuthenticatorsCollection(mock_cli_options_root)
        instance2 = AuthenticatorsCollection(mock_cli_options_root)
        assert instance1 is instance2, "AuthenticatorsCollection should be a singleton"

    def test_init_successful_authenticator_creation(self, authenticators_collection):
        assert (
            "mock" in authenticators_collection._authenticators
        ), "Authenticator should be created and added to the collection"

    def test_init_unsuccessful_authenticator_creation(self, mock_cli_options_root):
        # Mocking the ValidationError to simulate a configuration failure
        errors = [
            InitErrorDetails(
                **{
                    "loc": ("mock_field", "mock_field"),
                    "input": "mock_input",
                    "ctx": {"error": "error message"},
                    "type": "value_error",
                }
            )
        ]
        validation_error = ValidationError.from_exception_data("invalid config", errors)

        with patch.object(
            MockAuthenticator.config_model, "__call__", side_effect=validation_error
        ):
            AuthenticatorsCollection._instance = None
            authenticators_collection = AuthenticatorsCollection(mock_cli_options_root)
            assert (
                "mock" not in authenticators_collection._authenticators
            ), "Authenticator with invalid configuration should not be added to the collection"

    def test_len(self, authenticators_collection):
        assert len(authenticators_collection) == len(
            authenticators_collection._authenticators
        ), "__len__ method should return the number of authenticators"

    def test_getitem_by_key(self, authenticators_collection):
        assert (
            authenticators_collection["mock"] is not None
        ), "__getitem__ should return the authenticator for the given key"

    def test_getitem_by_index(self, authenticators_collection):
        assert (
            authenticators_collection[0] is not None
        ), "__getitem__ should return the authenticator for the given index"

    def test_getitem_key_error(self, authenticators_collection):
        with pytest.raises(UnknownAuthenticator):
            authenticators_collection["invalid"]

    def test_get_method(self, authenticators_collection):
        assert (
            authenticators_collection.get("mock") is not None
        ), "get method should return the authenticator for the given key"

    def test_get_method_key_error(self, authenticators_collection):
        with pytest.raises(UnknownAuthenticator):
            authenticators_collection.get("invalid")

    def test_iter(self, authenticators_collection):
        for authenticator in authenticators_collection:
            assert (
                authenticator.tag == "mock"
            ), "__iter__ should return the authenticators in the collection"

    def test_set_item(self, authenticators_collection):
        authenticators_collection["new"] = MockAuthenticator(MockAuthenticatorConfig())
        assert (
            authenticators_collection["new"] is not None
        ), "__setitem__ should add a new authenticator"

    def test_set_item_frozen(self, authenticators_collection):
        authenticators_collection.freeze()
        with pytest.raises(FrozenInstanceError):
            authenticators_collection["new"] = MockAuthenticator(
                MockAuthenticatorConfig()
            )
