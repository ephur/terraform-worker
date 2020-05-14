import os

import pytest

import worker


@pytest.fixture
def state():
    state = worker.main.State(
        args={
            "aws_access_key_id": "1234567890",
            "aws_secret_access_key": "1234567890",
            "aws_region": "us-west-2",
            "state_region": "us-west-2",
            "deployment": "test-0001",
            "s3_bucket": "test_s3_bucket",
            "s3_prefix": "terraform/test-0001",
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
def providers():
    prov_list = {"aws": {"vars": {"version": "1.3.37"}}}
    return prov_list
