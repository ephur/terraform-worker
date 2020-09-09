import filecmp
import glob
import os
from unittest import mock

import pytest

import tfworker.terraform


def mock_pipe_exec(args, stdin=None, cwd=None, env=None):
    return (0, "".encode(), "".encode())


class TestTerraform:
    def test_prep_modules(self, state):
        tfworker.terraform.prep_modules(state.args.repository_path, state.temp_dir)
        for test_file in [
            "/terraform-modules/test_a/test.tf",
            "/terraform-modules/test_b/test.tf",
        ]:
            src = state.args.repository_path + test_file
            dst = state.temp_dir + test_file
            assert filecmp.cmp(src, dst, shallow=False)

    def test_prep_def(self, state, definition, all_definitions):
        test_config_file = os.path.join(
            os.path.dirname(__file__), "fixtures/test_config.yaml"
        )
        state.load_config(test_config_file)
        body = state.config["terraform"]["definitions"]["test"]
        tfworker.terraform.prep_def(
            "test",
            body,
            state.config["terraform"],
            state.temp_dir,
            state.args.repository_path,
            "test-0001",
            state.args,
        )
        # File contents of rendered files are not tested, the rendering functions are tested in other tests
        assert os.path.isfile(state.temp_dir + "/definitions/test/test.tf")
        assert os.path.isfile(state.temp_dir + "/definitions/test/terraform.tf")
        assert os.path.isfile(state.temp_dir + "/definitions/test/worker.auto.tfvars")

    def test_plugin_download(self, state):
        tfworker.terraform.download_plugins({"aws": {"version": "1.9.0"}}, state.temp_dir)
        files = glob.glob(
            "{}/terraform-plugins/terraform-provider-aws_v1.9.0*".format(state.temp_dir)
        )
        assert len(files) > 0
        for afile in files:
            assert os.path.isfile(afile)
            assert (os.stat(afile).st_mode & 0o777) == 0o755

    @pytest.mark.parametrize(
        "base, expected",
        [({"terraform_vars": {"c": 1}}, 3), ({"miss": {"c": "bad_val"}}, 3), (None, 3)],
    )
    def test_make_vars(self, definition, base, expected):
        test_vars = tfworker.terraform.make_vars(
            "terraform_vars", definition["test"], base
        )
        assert test_vars["c"] == expected

    def test_render_remote_state(self, definition, state):
        deployment = state.args.deployment
        name = "test"
        render = tfworker.terraform.render_remote_state(name, deployment, state.args)
        expected_render = """terraform {
  backend "s3" {
    region = "us-west-2"
    bucket = "test_s3_bucket"
    key = "terraform/test-0001/test/terraform.tfstate"
    dynamodb_table = "terraform-test-0001"
    encrypt = "true"
  }
}"""
        assert render == expected_render

    def test_render_remote_data_sources(self, all_definitions, state):
        render = tfworker.terraform.render_remote_data_sources(
            all_definitions, "test2", state.args
        )
        expected_render = """data "terraform_remote_state" "test" {
  backend = "s3"
  config = {
    region = "us-west-2"
    bucket = "test_s3_bucket"
    key = "terraform/test-0001/test/terraform.tfstate"
  }
}
"""
        assert render == expected_render

    def test_render_providers(self, providers, state):
        render = tfworker.terraform.render_providers(providers, state.args)
        expected_render = """provider "aws" {
  version = "1.3.37"
}"""
        assert render == expected_render

    @pytest.mark.parametrize("method", ["init", "plan", "apply"])
    def test_run(self, definition, state, method):
        with mock.patch(
            "tfworker.terraform.pipe_exec", side_effect=mock_pipe_exec
        ) as mocked:
            name = "test"
            tfworker.terraform.run(
                name,
                "/tmp",
                "/usr/local/bin/terraform",
                method,
                state.args.aws_access_key_id,
                state.args.aws_secret_access_key,
            )
            mocked.assert_called_once()
