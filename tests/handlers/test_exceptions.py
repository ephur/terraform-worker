import pytest

from tfworker.handlers.exceptions import HandlerError, UnknownHandler


def test_handler_error():
    error_message = "This is a test error message"
    terminate = True

    error = HandlerError(error_message, terminate)

    assert error.message == error_message
    assert error.terminate == terminate
    assert str(error) == f"Handler error: {error_message}"


def test_handler_error_no_terminate():
    error_message = "This is a test error message"
    terminate = False

    error = HandlerError(error_message, terminate)

    assert error.message == error_message
    assert error.terminate == terminate
    assert str(error) == f"Handler error: {error_message}"


def test_unknown_handler():
    provider = "aws"

    error = UnknownHandler(provider)

    assert error.provider == provider
    assert str(error) == f"Unknown handler: {provider}"
