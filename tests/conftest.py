import os
import sys
from unittest.mock import MagicMock

import boto3
import click
import pytest
from moto import mock_aws

import tfworker.util.log as log
from tfworker.app_state import AppState
from tfworker.authenticators import AuthenticatorsCollection
from tfworker.backends import Backends
from tfworker.cli_options import CLIOptionsClean, CLIOptionsRoot, CLIOptionsTerraform
from tfworker.custom_types.config_file import ConfigFile, GlobalVars


@pytest.fixture(scope="function")
def aws_credentials():
    """Mocked AWS Credentials for moto."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


DEFINITIONS_SOURCE_SCRIPT = """#!/bin/sh
cat <<EOF
generated:
  template_vars:
    cwd: $(pwd)
    deployment: $WORKER_DEPLOYMENT
    inherited: $SOURCE_TEST_VAR
EOF
"""


@pytest.fixture(scope="function")
def definitions_source(tmp_path):
    """A local definitions source, tmp_path/catalog, with an executable bin/list-definitions."""
    src = tmp_path / "catalog"
    script = src / "bin" / "list-definitions"
    script.parent.mkdir(parents=True)
    script.write_text(DEFINITIONS_SOURCE_SCRIPT)
    script.chmod(0o755)
    return src


@pytest.fixture(scope="function")
def work_dir(tmp_path):
    """An empty working directory under tmp_path."""
    work = tmp_path / "work"
    work.mkdir()
    return work


@pytest.fixture(scope="function")
def empty_state():
    """A Representation of a terraform state file with no resources"""
    with open(f"{os.path.dirname(__file__)}/fixtures/states/empty.tfstate", "r") as f:
        return f.read()


@pytest.fixture(scope="function")
def occupied_state():
    """A Representation of a terraform state file with resources"""
    with open(
        f"{os.path.dirname(__file__)}/fixtures/states/occupied.tfstate", "r"
    ) as f:
        return f.read()


@pytest.fixture(scope="function")
def mock_cli_options_root():
    """A mock CLIOptionsRoot object with default values"""
    mock_root = MagicMock(spec=CLIOptionsRoot)
    mock_root.region = "us-east-1"
    mock_root.backend_region = "us-east-1"
    mock_root.backend_bucket = "test-bucket"
    mock_root.backend = Backends.S3
    mock_root.backend_plans = False
    mock_root.backend_prefix = "prefix"
    mock_root.create_backend_bucket = True
    mock_root.run_id = None
    mock_root.config_var = {}
    return mock_root


@pytest.fixture(scope="function")
def mock_cli_options_root_backend_west():
    """A mock CLIOptionsRoot object with default values and backend in us-west-2"""
    mock_root = MagicMock(spec=CLIOptionsRoot)
    mock_root.region = "us-east-1"
    mock_root.backend_region = "us-west-2"
    mock_root.backend_bucket = "west-test-bucket"
    mock_root.backend = Backends.S3
    mock_root.backend_plans = False
    mock_root.backend_prefix = "prefix"
    mock_root.create_backend_bucket = True
    mock_root.run_id = None
    return mock_root


@pytest.fixture(scope="function")
def mock_cli_options_terraform():
    """A mock CLIOptionsTerraform object with default values"""
    mock_terraform = MagicMock(spec=CLIOptionsTerraform)
    mock_terraform.apply = True
    mock_terraform.destroy = False
    mock_terraform.plan_file_path = None
    mock_terraform.force_plan = False
    return mock_terraform


@pytest.fixture(scope="function")
def mock_cli_options_clean():
    """A mock CLIOptionsClean object with default values"""
    mock_clean = MagicMock(spec=CLIOptionsClean)
    return mock_clean


@pytest.fixture(scope="function")
@mock_aws
def mock_authenticators(aws_credentials):
    """A mock AuthenticatorsCollection object with default values"""
    mock_auth = MagicMock(spec=AuthenticatorsCollection)
    mock_auth["aws"].session = boto3.Session()
    mock_auth["aws"].backend_session = mock_auth["aws"].session
    mock_auth["aws"].bucket = "test-bucket"
    mock_auth["aws"].prefix = "prefix"
    mock_auth["aws"].region = "us-east-1"
    mock_auth["aws"].backend_region = "us-east-1"
    return mock_auth


@pytest.fixture(scope="function")
@mock_aws
def mock_authenticators_backend_west(aws_credentials):
    """A mock AuthenticatorsCollection object with default values and backend in us-west-2"""
    mock_auth = MagicMock(spec=AuthenticatorsCollection)
    mock_auth["aws"].session = boto3.Session()
    mock_auth["aws"].backend_session = boto3.Session(region_name="us-west-2")
    mock_auth["aws"].bucket = "west-test-bucket"
    mock_auth["aws"].prefix = "prefix"
    mock_auth["aws"].region = "us-east-1"
    mock_auth["aws"].backend_region = "us-west-2"
    return mock_auth


@pytest.fixture
def mock_loaded_config():
    """A mock ConfigFile object with default values"""
    mock_config = MagicMock(spec=ConfigFile)
    return mock_config


@pytest.fixture(scope="function")
def mock_app_state(
    mock_cli_options_root,
    mock_cli_options_clean,
    mock_cli_options_terraform,
    mock_loaded_config,
    mock_authenticators,
    tmpdir,
):
    """A mock AppState object with default values"""
    mock_state = MagicMock(spec=AppState)
    mock_state.authenticators = mock_authenticators
    mock_state.root_options = mock_cli_options_root
    mock_state.deployment = "test-deployment"
    mock_state.clean_options = mock_cli_options_clean
    mock_state.terraform_options = mock_cli_options_terraform
    mock_state.loaded_config = mock_loaded_config
    mock_state.loaded_config.providers = {}
    mock_state.loaded_config.definitions = {}
    mock_state.loaded_config.handlers = {}
    mock_state.loaded_config.worker_options = {}
    mock_state.loaded_config.global_vars = GlobalVars()
    mock_state.working_dir = str(tmpdir)
    mock_state.terraform_version = (1, 13)

    return mock_state


@pytest.fixture(scope="function")
def mock_app_state_backend_west(
    mock_cli_options_root_backend_west,
    mock_cli_options_terraform,
    mock_cli_options_clean,
    mock_loaded_config,
    mock_authenticators,
    tmpdir,
):
    """A mock AppState object with default values and backend in us-west-2"""
    mock_state = MagicMock(spec=AppState)
    mock_state.authenticators = mock_authenticators
    mock_state.root_options = mock_cli_options_root_backend_west
    mock_state.clean_options = mock_cli_options_clean
    mock_state.terraform_options = mock_cli_options_terraform
    mock_state.deployment = "test-deployment"
    mock_state.loaded_config = mock_loaded_config
    mock_state.loaded_config.providers = {}
    mock_state.loaded_config.definitions = {}
    mock_state.loaded_config.handlers = {}
    mock_state.working_dir = str(tmpdir)
    mock_state.terraform_version = (1, 13)
    return mock_state


@pytest.fixture(scope="function")
def mock_click_context(mock_app_state):
    """A mock click context object with default values"""
    ctx = MagicMock(spec=click.Context)
    ctx.obj = mock_app_state
    ctx.exit = MagicMock(side_effect=sys.exit)
    return ctx


@pytest.fixture(scope="function")
def mock_click_context_backend_west(mock_app_state_backend_west):
    """A mock click context object with default values and backend in us-west-2"""
    ctx = MagicMock(spec=click.Context)
    ctx.obj = mock_app_state_backend_west
    ctx.exit = MagicMock(side_effect=sys.exit)
    return ctx


@pytest.fixture(autouse=True)
def setup_method(mocker, mock_click_context):
    """A fixture to setup the click context which is used throughout"""
    mocker.patch("click.get_current_context", return_value=mock_click_context)


@pytest.fixture(autouse=True)
def reset_log_globals():
    """Restore the log module's globals after every test.

    Level, format, and run context are module state; a test that switches to
    JSON logging (or fails before restoring it) would otherwise change how
    every later test behaves.
    """
    level, fmt = log.log_level, log.log_format
    context = dict(log._run_context)
    yield
    log.log_level, log.log_format = level, fmt
    log.clear_context()
    log.set_context(**context)
