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

  required_providers {

  }
}"""

EXPECTED_VARS_BLOCK = """vpc_cidr = "10.0.0.0/16"
region = "us-west-2"
deprecated_region = "us-west-2"
domain = "test.domain.com"
ip_list = ["127.0.0.1/32", "192.168.0.1/32"]
deployment = "test-0001"
"""


class TestDefinitions:
    def test_prep(self, basec):
        definition = basec.definitions["test"]
        definition.prep(basec.backend)
        # File contents of rendered files are not tested, the rendering functions are tested in other tests
        assert os.path.isfile(basec.temp_dir + "/definitions/test/test.tf")
        with open(basec.temp_dir + "/definitions/test/test.tf", "r") as reader:
            assert EXPECTED_TEST_BLOCK in reader.read()
        assert os.path.isfile(basec.temp_dir + "/definitions/test/terraform.tf")
        with open(basec.temp_dir + "/definitions/test/terraform.tf", "r") as reader:
            assert EXPECTED_TF_BLOCK in reader.read()
        assert os.path.isfile(basec.temp_dir + "/definitions/test/worker.auto.tfvars")
        with open(
            basec.temp_dir + "/definitions/test/worker.auto.tfvars", "r"
        ) as reader:
            assert EXPECTED_VARS_BLOCK in reader.read()

    @pytest.mark.parametrize(
        "base, expected",
        [({"terraform_vars": {"c": 1}}, 3), ({"miss": {"c": "bad_val"}}, 3), ({}, 3)],
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
        )
        test_vars = definition.make_vars(
            definition_odict[name].get("terraform_vars", collections.OrderedDict()),
            base.get("terraform_vars"),
        )
        assert test_vars["c"] == expected
