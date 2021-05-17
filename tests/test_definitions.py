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

import collections
import os

import pytest
from tfworker.definitions import Definition

EXPECTED_TEST_BLOCK = """resource "null_resource" "test_a" {

}
"""

EXPECTED_TF_BLOCK = """terraform {
  backend "s3" {
    region = "us-west-2"
    bucket = "test_bucket"
    key = "terraform/test-0001/test/terraform.tfstate"
    dynamodb_table = "terraform-test-0001"
    encrypt = "true"
  }
}"""


EXPECTED_VARS_BLOCK = """vpc_cidr = "10.0.0.0/16"
region = "us-west-2"
deprecated_region = "us-west-2"
domain = "test.domain.com"
ip_list = ["127.0.0.1/32", "192.168.0.1/32"]
map_list = {"list": ["a", "b", "c"]}
map_map = {"map": {"list": ["x", "y", "z"]}}
deployment = "test-0001"
"""


class TestDefinitions:
    @pytest.mark.parametrize(
        "tf_version, expected_tf_block, expected_providers",
        [
            (15, EXPECTED_TF_BLOCK, ["google", "null"]),
            (14, EXPECTED_TF_BLOCK, ["google", "null"]),
            (13, EXPECTED_TF_BLOCK, ["google", "null"]),
            (12, EXPECTED_TF_BLOCK, ["aws", "google", "google_beta", "null", "vault"]),
        ],
    )
    def test_prep(self, basec, tf_version, expected_tf_block, expected_providers):
        definition = basec.definitions["test"]
        definition._tf_version_major = tf_version
        definition.prep(basec.backend)
        # File contents of rendered files are not tested, the rendering functions are tested in other tests
        assert os.path.isfile(basec.temp_dir + "/definitions/test/test.tf")
        with open(basec.temp_dir + "/definitions/test/test.tf", "r") as reader:
            assert EXPECTED_TEST_BLOCK in reader.read()
        assert os.path.isfile(basec.temp_dir + "/definitions/test/terraform.tf")
        with open(basec.temp_dir + "/definitions/test/terraform.tf", "r") as reader:
            tf_data = reader.read()
            assert expected_tf_block in tf_data
            for ep in expected_providers:
                assert f'provider "{ep}" {{' in tf_data
        assert os.path.isfile(basec.temp_dir + "/definitions/test/worker.auto.tfvars")
        with open(
            basec.temp_dir + "/definitions/test/worker.auto.tfvars", "r"
        ) as reader:
            assert EXPECTED_VARS_BLOCK in reader.read()

    @pytest.mark.parametrize(
        "base, expected",
        [
            ({"terraform_vars": {"c": 1}}, 3),
            ({"miss": {"c": "bad_val"}}, 3),
            ({}, 3),
        ],
    )
    def test_make_vars(self, definition_odict, base, expected):
        name = "test"
        definition = Definition(
            name,
            definition_odict[name],
            "test_deployment",
            {},
            {},
            {},
            None,
            "",
            "",
            12,
        )
        test_vars = definition.make_vars(
            definition_odict[name].get("terraform_vars", collections.OrderedDict()),
            base.get("terraform_vars"),
        )
        assert test_vars["c"] == expected

    @pytest.mark.parametrize(
        "base, expected, inner",
        [
            ("a_test_str", '"a_test_str"', False),
            (
                {"key1": "val1", "key2": "val2"},
                '{"key1": "val1", "key2": "val2"}',
                False,
            ),
            ({"key1": "val1", "key2": "val2"}, {"key1": "val1", "key2": "val2"}, True),
            (["item1", "item2", "item3"], '["item1", "item2", "item3"]', False),
            (["item1", "item2", "item3"], ["item1", "item2", "item3"], True),
            (
                {"lkey": ["item1", "item2", "item3"]},
                '{"lkey": ["item1", "item2", "item3"]}',
                False,
            ),
            (
                {"lkey": ["item1", "item2", "item3"]},
                {"lkey": ["item1", "item2", "item3"]},
                True,
            ),
        ],
    )
    def test_var_typer(self, base, expected, inner):
        assert Definition.vars_typer(base, inner=inner) == expected
