# Copyright 2020 Richard Maynard (richard.maynard@gmail.com)
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

import copy
import os
import platform
from tempfile import TemporaryDirectory
from unittest import mock

import pytest
from deepdiff import DeepDiff

import tfworker.commands.root
from tfworker.commands.root import get_platform


class TestMain:
    def test_rc_add_arg(self, rootc):
        rc = copy.deepcopy(rootc)
        rc.add_arg("a", 1)
        assert rc.args.a == 1

    def test_rc_add_args(self, rootc):
        rc = copy.deepcopy(rootc)
        rc.add_args({"a": 1, "b": "two"})
        assert rc.args.a == 1
        assert rc.args.b == "two"

    def test_rc_init(self, rootc):
        rc = tfworker.commands.root.RootCommand(args={"a": 1, "b": "two"})
        assert rc.args.a == 1
        assert rc.args.b == "two"

    def test_rc_init_clean(self, rootc):
        # by default clean should be true
        rc = tfworker.commands.root.RootCommand()
        assert rc.clean is True

        # if working_dir is passed, clean should be false
        rc = tfworker.commands.root.RootCommand(args={"working_dir": "/tmp"})
        assert rc.clean is False
        if platform.system() == "Darwin":
            assert str(rc.temp_dir) == "/private/tmp"
        else:
            assert str(rc.temp_dir) == "/tmp"

        # if clean is passed, it should be set to the value passed
        rc = tfworker.commands.root.RootCommand(args={"clean": False})
        assert rc.temp_dir is not None
        assert rc.clean is False

        # if a working dir is specified, along with clean, the dir itself
        # should not be deleted, but contents inside of it should
        tmpdir = TemporaryDirectory()
        assert os.path.exists(tmpdir.name) is True
        rc = tfworker.commands.root.RootCommand(
            args={"clean": True, "working_dir": tmpdir.name}
        )
        assert rc.clean is True
        if platform.system() == "Darwin":
            assert str(rc.temp_dir) == f"/private{tmpdir.name}"
        else:
            assert str(rc.temp_dir) == tmpdir.name
        with open(file=os.path.join(tmpdir.name, "test"), mode="w") as f:
            f.write("test")
        del rc
        assert os.path.exists(os.path.join(tmpdir.name, "test")) is False
        assert len(os.listdir(tmpdir.name)) == 0
        tmpdir.cleanup()

    def test_config_loader(self, rootc, capfd):
        expected_sections = ["providers", "terraform_vars", "definitions"]
        expected_tf_vars = {
            "vpc_cidr": "10.0.0.0/16",
            "region": "us-west-2",
            "domain": "test.domain.com",
        }
        rootc.add_arg("deployment", "root-deployment")
        rootc.load_config()
        terraform_config = rootc.config.get("terraform")
        for section in expected_sections:
            assert section in terraform_config.keys()

        for k, v in expected_tf_vars.items():
            assert terraform_config["terraform_vars"][k] == v

        # a root command with no config should return None
        emptyrc = tfworker.commands.root.RootCommand()
        assert emptyrc.load_config() is None

        # an invalid path should raise an error
        invalidrc = tfworker.commands.root.RootCommand({"config_file": "/tmp/invalid"})
        with pytest.raises(SystemExit) as e:
            invalidrc.load_config()
        assert e.value.code == 1
        out, err = capfd.readouterr()
        assert "can not read" in out

        # a j2 template with invalid substitutions should raise an error
        invalidrc = tfworker.commands.root.RootCommand(
            {
                "config_file": os.path.join(
                    os.path.dirname(__file__),
                    "..",
                    "fixtures",
                    "test_config_invalid_j2.yaml",
                )
            }
        )
        with pytest.raises(SystemExit) as e:
            invalidrc.load_config()
        assert e.value.code == 1
        out, err = capfd.readouterr()
        assert "invalid template" in out

    def test_pullup_keys_edge(self):
        rc = tfworker.commands.root.RootCommand()
        assert rc.load_config() is None
        assert rc._pullup_keys() is None
        assert rc.providers_odict is None

    def test_get_config_var_dict(self):
        config_vars = ["foo=bar", "this=that", "one=two"]
        result = tfworker.commands.root.get_config_var_dict(config_vars)
        assert len(result) == 3
        assert result["foo"] == "bar"
        assert result["this"] == "that"
        assert result["one"] == "two"

    def test_stateargs_base(self):
        rc = tfworker.commands.root.RootCommand.StateArgs()
        setattr(rc, "foo", "bar")
        setattr(rc, "this", ["that", "thing"])
        setattr(rc, "one", 2)
        assert rc.foo == "bar"
        assert len(rc.this) == 2
        assert rc.one == 2
        assert rc["foo"] == "bar"
        assert rc["this"] == ["that", "thing"]
        assert rc["one"] == 2

        for k in rc.keys():
            assert k in ["foo", "this", "one"]

        for k in rc.values():
            assert k in ["bar", ["that", "thing"], 2]

        for k in rc:
            assert k in ["foo", "this", "one"]

        assert str(rc) == "{'foo': 'bar', 'this': ['that', 'thing'], 'one': 2}"

        for k, v in rc.items():
            assert k in ["foo", "this", "one"]
            assert v in ["bar", ["that", "thing"], 2]

    def test_stateargs_template_items(self):
        rc = tfworker.commands.root.RootCommand.StateArgs()
        # push --config-var param onto rootcommand
        setattr(rc, "config_var", ["foo=bar", "this=that", "one=two"])

        # check templating of config_var, no environment
        result = rc.template_items(return_as_dict=True)
        assert result["var"] == {"foo": "bar", "this": "that", "one": "two"}
        with pytest.raises(KeyError):
            result["env"]

        # check templating of config_var, with environment
        os.environ["FOO"] = "bar"
        os.environ["THIS"] = "that"
        os.environ["ONE"] = "two"
        result = rc.template_items(return_as_dict=True, get_env=True)
        assert result["env"]["FOO"] == "bar"
        assert result["env"]["THIS"] == "that"
        assert result["env"]["ONE"] == "two"

        # check templating when returning as a list
        result = rc.template_items(return_as_dict=False, get_env=True)
        for k, v in result:
            assert k in ["var", "env"]
            if k == "var":
                assert v == {"foo": "bar", "this": "that", "one": "two"}
            if k == "env":
                assert v["FOO"] == "bar"
                assert v["THIS"] == "that"
                assert v["ONE"] == "two"

    def test_stateargs_template_items_invalid(self, capfd):
        rc = tfworker.commands.root.RootCommand.StateArgs()
        # push --config-var param onto rootcommand
        setattr(rc, "config_var", ["junky"])

        # check templating of config_var, no environment
        with pytest.raises(SystemExit) as e:
            result = rc.template_items(return_as_dict=True)
        out, err = capfd.readouterr()
        assert e.value.code == 1
        assert "Invalid config-var" in out

    def test_config_formats(self, yaml_base_rootc, json_base_rootc, hcl_base_rootc):
        yaml_base_rootc.load_config()
        json_base_rootc.load_config()
        hcl_base_rootc.load_config()
        yaml_config = yaml_base_rootc.config
        json_config = json_base_rootc.config
        hcl_config = hcl_base_rootc.config
        diff = DeepDiff(yaml_config, json_config)
        assert len(diff) == 0
        diff = DeepDiff(json_config, hcl_config)
        assert len(diff) == 0

    @pytest.mark.parametrize(
        "opsys, machine, mock_platform_opsys, mock_platform_machine",
        [
            ("linux", "i386", ["linux2"], ["i386"]),
            ("linux", "arm", ["Linux"], ["arm"]),
            ("linux", "amd64", ["linux"], ["x86_64"]),
            ("linux", "amd64", ["linux"], ["amd64"]),
            ("darwin", "amd64", ["darwin"], ["x86_64"]),
            ("darwin", "amd64", ["darwin"], ["amd64"]),
            ("darwin", "arm", ["darwin"], ["arm"]),
            ("darwin", "arm64", ["darwin"], ["aarch64"]),
        ],
    )
    def test_get_platform(
        self, opsys, machine, mock_platform_opsys, mock_platform_machine
    ):
        with mock.patch("platform.system", side_effect=mock_platform_opsys) as mock1:
            with mock.patch(
                "platform.machine", side_effect=mock_platform_machine
            ) as mock2:
                actual_opsys, actual_machine = get_platform()
                assert opsys == actual_opsys
                assert machine == actual_machine
                mock1.assert_called_once()
                mock2.assert_called_once()
