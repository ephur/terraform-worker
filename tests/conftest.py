import os

import pytest

import tfworker
import tfworker.main


@pytest.fixture
def state():
    state = tfworker.main.State(
        args={
            "aws_access_key_id": "1234567890",
            "aws_secret_access_key": "1234567890",
            "aws_region": "us-west-2",
            "backend": "s3",
            "backend_region": "us-west-2",
            "deployment": "test-0001",
            "s3_bucket": "test_s3_bucket",
            "s3_prefix": "terraform/test-0001",
            "repository_path": os.path.join(os.path.dirname(__file__), "fixtures"),
        }
    )
    return state


@pytest.fixture
def gcs_backend_state():
    state = tfworker.main.State(
        args={
            "gcp_region": "us-west-2b",
            "gcp_bucket": "test_gcp_bucket",
            "gcp_prefix": "terraform/test-0002",
            "gcp_creds_path": "/home/test/test-creds.json",
            "backend": "gcs",
            "backend_region": "us-west-2b",
            "deployment": "test-0001",
            "repository_path": os.path.join(os.path.dirname(__file__), "fixtures"),
        }
    )
    return state


@pytest.fixture
def definition():
    one_def = {
        "test": {
            "path": "/test",
            "remote_vars": {"a": 1, "b": "two"},
            "terraform_vars": {"c": 3, "d": "four"},
            "template_vars": {"e": 5, "f": "six"},
        }
    }
    return one_def


@pytest.fixture
def all_definitions():
    all_defs = {
        "test": {
            "path": "/test",
            "remote_vars": {"a": 1, "b": "two"},
            "terraform_vars": {"c": 3, "d": "four"},
            "template_vars": {"e": 5, "f": "six"},
        },
        "test2": {
            "path": "/test2",
            "remote_vars": {"g": 7, "h": "eight"},
            "terraform_vars": {"i": 9, "j": "ten"},
            "template_vars": {"k": 11, "l": "eleven"},
        },
    }
    return all_defs


@pytest.fixture
def aws_provider():
    prov_list = {"aws": {"vars": {"version": "1.3.37"}}}
    return prov_list


@pytest.fixture
def google_provider():
    prov_list = {"google": {"vars": {"version": "3.38.0"}}}
    return prov_list
