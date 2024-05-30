# Copyright 2020-2023 Richard Maynard (richard.maynard@gmail.com)
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
deployment = "test-0001"
ip_list = ["127.0.0.1/32", "192.168.0.1/32"]
map_list = {"list": ["a", "b", "c"]}
map_map = {"map": {"list": ["x", "y", "z"]}}
"""


class TestDefinitions:
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
            definition_odict[name].get("terraform_vars", dict()),
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
