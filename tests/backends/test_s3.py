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


def test_s3_hcl(basec):
    render = basec.backend.hcl("test")
    expected_render = """terraform {
  backend "s3" {
    region = "us-west-2"
    bucket = "test_bucket"
    key = "terraform/test-0001/test/terraform.tfstate"
    dynamodb_table = "terraform-test-0001"
    encrypt = "true"
  }
}"""
    assert render == expected_render


def test_s3_data_hcl(basec):
    render = basec.backend.data_hcl("test2")
    expected_render = """data "terraform_remote_state" "test" {
  backend = "s3"
  config = {
    region = "us-west-2"
    bucket = "test_bucket"
    key = "terraform/test-0001/test/terraform.tfstate"
  }
}
"""
    assert render == expected_render
