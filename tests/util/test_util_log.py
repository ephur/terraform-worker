from unittest.mock import patch

import pytest

import tfworker.util.log as log

REDACTED_ITEMS = ["aws_secret_access_key", "aws_session_token", "aws_profile"]


@pytest.fixture(autouse=True)
def reset_log_level():
    """Reset log level to ERROR after each test"""
    log.log_level = log.LogLevel.ERROR
    yield
    log.log_level = log.LogLevel.ERROR


def test_redact_items_re_string():
    sensitive_string = """aws_secret_access_key="my_secret_key" aws_session_token 'my_session_token' aws_profile: 'default'     aws_profile=admin_profile"""
    expected_result = """aws_secret_access_key="REDACTED" aws_session_token 'REDACTED' aws_profile: 'REDACTED'     aws_profile=REDACTED"""
    assert (
        log.redact_items_re(sensitive_string, redact=REDACTED_ITEMS) == expected_result
    )


def test_redact_items_re_dict():
    sensitive_dict = {
        "aws_secret_access_key": "my_secret_key",
        "aws_session_token": "my_session_token",
        "note": "aws_profile=admin_profile",
    }
    expected_result = {
        "aws_secret_access_key": "REDACTED",
        "aws_session_token": "REDACTED",
        "note": "aws_profile=REDACTED",
    }
    assert log.redact_items_re(sensitive_dict, redact=REDACTED_ITEMS) == expected_result


def test_redact_items_re_invalid_type():
    with pytest.raises(ValueError, match="Items must be a dictionary or a string"):
        log.redact_items_re(12345)


def test_redact_items_re_nested_dict():
    sensitive_dict = {
        "level1": {
            "aws_secret_access_key": "my_secret_key",
            "nested": {
                "aws_session_token": "my_session_token",
                "note": "aws_profile=admin_profile",
            },
        }
    }
    expected_result = {
        "level1": {
            "aws_secret_access_key": "REDACTED",
            "nested": {"aws_session_token": "REDACTED", "note": "aws_profile=REDACTED"},
        }
    }
    assert log.redact_items_re(sensitive_dict, redact=REDACTED_ITEMS) == expected_result


def test_redact_items_re_for_overredaction():
    sensitive_string = """
terraform:
  worker_options:
    aws_region: us-east-1
    aws_profile: default
    backend: s3
    backend_bucket: test-terraform
    backend_region: us-west-2
    provider-cache: ./.cache


  definitions:
    test:
      path: ./dns

    argo_test:
      path: ./argo_test

  providers:
    aws:
      requirements:
        version: 5.54.1
      config_blocks:
        default_tags:
          tags:
            terraform: "true"
            deployment: foo
    'null':
      requirements:
        version: 3.2.2
"""
    expected_result = """
terraform:
  worker_options:
    aws_region: us-east-1
    aws_profile: REDACTED
    backend: s3
    backend_bucket: test-terraform
    backend_region: us-west-2
    provider-cache: ./.cache


  definitions:
    test:
      path: ./dns

    argo_test:
      path: ./argo_test

  providers:
    aws:
      requirements:
        version: 5.54.1
      config_blocks:
        default_tags:
          tags:
            terraform: "true"
            deployment: foo
    'null':
      requirements:
        version: 3.2.2
"""
    assert (
        log.redact_items_re(sensitive_string, redact=REDACTED_ITEMS) == expected_result
    )


def test_redact_items_token_string():
    sensitive_string = """aws_secret_access_key="my_secret_key" aws_session_token 'my_session_token' aws_profile: 'default' aws_profile=admin_profile"""
    expected_result = """aws_secret_access_key="REDACTED" aws_session_token 'REDACTED' aws_profile: 'REDACTED' aws_profile=REDACTED"""
    assert (
        log.redact_items_token(sensitive_string, redact=REDACTED_ITEMS)
        == expected_result
    )


def test_redact_items_token_dict():
    sensitive_dict = {
        "aws_secret_access_key": "my_secret_key",
        "aws_session_token": "my_session_token",
        "note": "aws_profile=admin_profile",
    }
    expected_result = {
        "aws_secret_access_key": "REDACTED",
        "aws_session_token": "REDACTED",
        "note": "aws_profile=REDACTED",
    }
    assert (
        log.redact_items_token(sensitive_dict, redact=REDACTED_ITEMS) == expected_result
    )


def test_redact_items_token_invalid_type():
    with pytest.raises(ValueError, match="Items must be a dictionary or a string"):
        log.redact_items_token(12345)


def test_redact_items_token_nested_dict():
    sensitive_dict = {
        "level1": {
            "aws_secret_access_key": "my_secret_key",
            "nested": {
                "aws_session_token": "my_session_token",
                "note": "aws_profile=admin_profile",
            },
        }
    }
    expected_result = {
        "level1": {
            "aws_secret_access_key": "REDACTED",
            "nested": {"aws_session_token": "REDACTED", "note": "aws_profile=REDACTED"},
        }
    }
    assert (
        log.redact_items_token(sensitive_dict, redact=REDACTED_ITEMS) == expected_result
    )


def test_redact_items_token_for_overredaction():
    sensitive_string = """
terraform:
  worker_options:
    aws_region: us-east-1
    aws_profile: default
    backend: s3
    backend_bucket: test-terraform
    backend_region: us-west-2
    provider-cache: ./.cache


  definitions:
    test:
      path: ./dns

    argo_test:
      path: ./argo_test

  providers:
    aws:
      requirements:
        version: 5.54.1
      config_blocks:
        default_tags:
          tags:
            terraform: "true"
            deployment: foo
    'null':
      requirements:
        version: 3.2.2
"""
    expected_result = """
terraform:
  worker_options:
    aws_region: us-east-1
    aws_profile: REDACTED
    backend: s3
    backend_bucket: test-terraform
    backend_region: us-west-2
    provider-cache: ./.cache


  definitions:
    test:
      path: ./dns

    argo_test:
      path: ./argo_test

  providers:
    aws:
      requirements:
        version: 5.54.1
      config_blocks:
        default_tags:
          tags:
            terraform: "true"
            deployment: foo
    'null':
      requirements:
        version: 3.2.2
"""
    assert (
        log.redact_items_token(sensitive_string, redact=REDACTED_ITEMS)
        == expected_result
    )


@patch("tfworker.util.log.secho")
def test_log_no_redaction(mock_secho):
    log.log_level = log.LogLevel.INFO
    log.log("This is a test message.", log.LogLevel.INFO)
    mock_secho.assert_called_once_with("This is a test message.", fg="green")


@patch("tfworker.util.log.secho")
def test_log_with_redaction(mock_secho):
    log.log_level = log.LogLevel.INFO
    sensitive_string = """aws_secret_access_key="my_secret_key" aws_session_token 'my_session_token' aws_session_token:my_session_token"""
    expected_result = """aws_secret_access_key="REDACTED" aws_session_token 'REDACTED' aws_session_token:REDACTED"""
    log.log(sensitive_string, log.LogLevel.INFO, redact=True)
    mock_secho.assert_called_once_with(expected_result, fg="green")


@patch("tfworker.util.log.secho")
def test_partial_safe_info(mock_secho):
    log.log_level = log.LogLevel.INFO
    sensitive_string = (
        """aws_secret_access_key="my_secret_key" aws_session_token my_session_token"""
    )
    expected_result = """aws_secret_access_key="REDACTED" aws_session_token REDACTED"""
    log.safe_info(sensitive_string)
    mock_secho.assert_called_once_with(expected_result, fg="green")


@patch("tfworker.util.log.secho")
def test_partial_info_no_redaction(mock_secho):
    log.log_level = log.LogLevel.INFO
    message = "This is an info message."
    log.info(message)
    mock_secho.assert_called_once_with(message, fg="green")


@patch("tfworker.util.log.secho")
def test_log_levels(mock_secho):
    log.log_level = log.LogLevel.DEBUG

    trace_message = "This is a trace message."
    debug_message = "This is a debug message."
    info_message = "This is an info message."
    warn_message = "This is a warn message."
    error_message = "This is an error message."

    log.trace(trace_message)
    assert not mock_secho.called  # TRACE should not appear since log_level is DEBUG

    log.debug(debug_message)
    mock_secho.assert_called_with(debug_message, fg="blue")

    log.info(info_message)
    mock_secho.assert_called_with(info_message, fg="green")

    log.warn(warn_message)
    mock_secho.assert_called_with(warn_message, fg="yellow")

    log.error(error_message)
    mock_secho.assert_called_with(error_message, fg="red")

    log.log_level = log.LogLevel.TRACE

    log.trace(trace_message)
    mock_secho.assert_called_with(
        trace_message, fg="cyan"
    )  # TRACE should appear since log_level is TRACE


@patch("tfworker.util.log.secho")
def test_log_with_redaction_and_error_level(mock_secho):
    log.log_level = log.LogLevel.INFO
    sensitive_string = "Error: aws_secret_access_key=my_secret_key"
    expected_result = "Error: aws_secret_access_key=REDACTED"
    log.log(sensitive_string, log.LogLevel.ERROR, redact=True)
    mock_secho.assert_called_once_with(expected_result, fg="red")


@patch("tfworker.util.log.secho")
def test_log_trace_level(mock_secho):
    log.log_level = log.LogLevel.TRACE
    log.log("This is a trace message.", log.LogLevel.TRACE)
    mock_secho.assert_called_once_with("This is a trace message.", fg="cyan")


# performance testing the two different redact methods
@pytest.mark.performance
def test_redact_items_regex_performance():
    import timeit

    iterations = 200000
    sensitive_string = """aws_secret_access_key="my_secret_key" aws_session_token 'my_session_token' aws_profile: 'default' aws_profile=admin_profile"""
    elapsed_time = timeit.timeit(
        lambda: log.redact_items_re(sensitive_string), number=iterations
    )
    print(
        f"Regex implementation took {elapsed_time:.4f} seconds for {iterations} iterations"
    )


@pytest.mark.performance
def test_redact_items_tokenize_performance():
    import timeit

    iterations = 200000
    sensitive_string = """aws_secret_access_key="my_secret_key" aws_session_token 'my_session_token' aws_profile: 'default' aws_profile=admin_profile"""
    elapsed_time = timeit.timeit(
        lambda: log.redact_items_token(sensitive_string), number=iterations
    )
    print(
        f"Tokenize implementation took {elapsed_time:.4f} seconds for {iterations} iterations"
    )


if __name__ == "__main__":
    pytest.main()
