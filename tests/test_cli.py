# Copyright 2023 Richard Maynard (richard.maynard@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import tempfile
from unittest import mock
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import tfworker.cli
from tfworker.commands import CleanCommand


class TestCLI:
    def test_validate_deployment_valid(self):
        """ensure valid names are returned"""
        assert tfworker.cli.validate_deployment(None, None, "test") == "test"

    def test_validate_deployment_invalid_spaces(self, capfd):
        """ensure deploys with spaces failed"""
        with pytest.raises(SystemExit) as e:
            tfworker.cli.validate_deployment(None, None, "test test")
        out, err = capfd.readouterr()
        assert e.type == SystemExit
        assert e.value.code == 1
        assert "not contain spaces" in out

    def test_validate_deployment_invalid_length(self, capfd):
        """ensure deploy over 16 chars fail"""
        with pytest.raises(SystemExit) as e:
            tfworker.cli.validate_deployment(
                None, None, "testtesttesttesttesttesttesttesttesttest"
            )
        out, err = capfd.readouterr()
        assert e.type == SystemExit
        assert e.value.code == 1
        assert "32 characters" in out

    def test_validate_gcp_creds_path(self):
        """ensure valid creds paths are returned"""
        with tempfile.NamedTemporaryFile(mode="w+") as tmpf:
            assert (
                tfworker.cli.validate_gcp_creds_path(None, None, tmpf.name) == tmpf.name
            )

    def test_validate_gcp_creds_path_invalid(self, capfd):
        """ensure invalid creds paths fail"""
        with pytest.raises(SystemExit) as e:
            tfworker.cli.validate_gcp_creds_path(None, None, "test")
        out, err = capfd.readouterr()
        assert e.type == SystemExit
        assert e.value.code == 1
        assert "not resolve GCP credentials" in out

    def test_validate_host(self):
        """only linux and darwin are supported, and require 64 bit platforms"""
        with patch("tfworker.cli.get_platform", return_value=("linux", "amd64")):
            assert tfworker.cli.validate_host() is True
        with patch("tfworker.cli.get_platform", return_value=("darwin", "amd64")):
            assert tfworker.cli.validate_host() is True
        with patch("tfworker.cli.get_platform", return_value=("darwin", "arm64")):
            assert tfworker.cli.validate_host() is True

    def test_validate_host_invalid_machine(self, capfd):
        """ensure invalid machine types fail"""
        with patch("tfworker.cli.get_platform", return_value=("darwin", "i386")):
            with pytest.raises(SystemExit) as e:
                tfworker.cli.validate_host()
            out, err = capfd.readouterr()
            assert e.type == SystemExit
            assert e.value.code == 1
            assert "not supported" in out

    def test_validate_host_invalid_os(self, capfd):
        """ensure invalid os types fail"""
        with patch("tfworker.cli.get_platform", return_value=("windows", "amd64")):
            with pytest.raises(SystemExit) as e:
                tfworker.cli.validate_host()
            out, err = capfd.readouterr()
            assert e.type == SystemExit
            assert e.value.code == 1
            assert "not supported" in out

    def test_validate_working_dir(self):
        """ensure valid working dirs are returned"""
        assert tfworker.cli.validate_working_dir(None) is None

        with tempfile.TemporaryDirectory() as tmpd:
            assert tfworker.cli.validate_working_dir(tmpd) is None

    def test_validate_working_dir_is_file(self, capfd):
        """ensure files fail"""
        with tempfile.NamedTemporaryFile(mode="w+") as tmpf:
            with pytest.raises(SystemExit) as e:
                tfworker.cli.validate_working_dir(tmpf.name)
            out, err = capfd.readouterr()
            assert e.type == SystemExit
            assert e.value.code == 1
            assert "not a directory" in out

    def test_validate_working_dir_does_not_exist(self, capfd):
        """ensure non existent dirs fail"""
        with pytest.raises(SystemExit) as e:
            tfworker.cli.validate_working_dir("test")
        out, err = capfd.readouterr()
        assert e.type == SystemExit
        assert e.value.code == 1
        assert "does not exist" in out

    def test_validate_working_dir_not_empty(self, capfd):
        """ensure non empty dirs fail"""
        with tempfile.TemporaryDirectory() as tmpd:
            with open(os.path.join(tmpd, "test"), "w+") as tmpf:
                with pytest.raises(SystemExit) as e:
                    tfworker.cli.validate_working_dir(tmpd)
                out, err = capfd.readouterr()
                assert e.type == SystemExit
                assert e.value.code == 1
                assert "must be empty" in out

    def test_cli_no_params(self):
        """ensure cli returns usage with no params"""
        from tfworker.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli)
        assert result.exit_code == 0
        assert "Usage: cli [OPTIONS] COMMAND [ARGS]..." in result.output

    def test_cli_missing_command(self):
        """ensure cli returns usage with no command"""
        from tfworker.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--config-file", "test"])
        assert result.exit_code == 2
        assert "Missing command" in result.output

    def test_cli_invalid_config(self):
        """ensure the CLI fails with an invalid config file"""
        # @TODO(ephur): this test demonstrates an issue with how rendering exits
        #      when it encounters errors, this masks the true error of config
        #      file not being found.  This should be fixed in a future PR.
        from tfworker.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--config-file", "test", "terraform", "foo"])
        assert result.exit_code == 1
        # the expected result is: configuration file {config_file} not found" the
        # exception is being handled in the wrong place
        assert "configuration file does not exist" in result.output

    @patch("tfworker.cli.CleanCommand", autospec=True)
    def test_cli_clean_command(self, mock_request, test_config_file):
        """ensure the CLI clean command executes"""
        from tfworker.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--config-file", test_config_file, "clean", "foo"])
        mock_request.assert_called_once()
        assert mock_request.method_calls[0][0] == "().exec"

    @patch("tfworker.cli.VersionCommand", autospec=True)
    def test_cli_version_command(self, mock_request, test_config_file):
        """ensure the CLI version command executes"""
        from tfworker.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["version"])
        mock_request.assert_called_once()
        assert mock_request.method_calls[0][0] == "().exec"

    @patch("tfworker.cli.TerraformCommand", autospec=True)
    def test_cli_terraform_command(self, mock_request, test_config_file):
        """ensure the CLI terraform command executes
        @TODO(ephur): This test demonstrates why the CLI skel should only
        call the exec method of the command, and not the other methods.
        """
        from tfworker.cli import cli

        runner = CliRunner()
        result = runner.invoke(
            cli, ["--config-file", test_config_file, "terraform", "foo"]
        )
        assert result.exit_code == 0
        # the three steps the cli should execute
        assert mock_request.method_calls[0][0] == "().plugins.download"
        assert mock_request.method_calls[1][0] == "().prep_modules"
        assert mock_request.method_calls[2][0] == "().exec"

    @patch("tfworker.cli.EnvCommand", autospec=True)
    def test_cli_env_command(self, mock_request, test_config_file):
        """ensure the CLI env command executes"""
        from tfworker.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--config-file", test_config_file, "env"])
        mock_request.assert_called_once()
        assert mock_request.method_calls[0][0] == "().exec"
