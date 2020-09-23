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

import pytest
import tfworker


@pytest.fixture
def gbasec(rootc):
    _copy = copy.deepcopy(rootc)
    _copy.args.backend = "gcs"
    _copy.args.backend_bucket = "test_gcp_bucket"
    _copy.args.backend_prefix = "terraform/test-0002"
    return tfworker.commands.base.BaseCommand(_copy, "test-0001-gcs")


def test_google_hcl(gbasec):
    render = gbasec.backend.hcl("test")
    expected_render = """terraform {
  backend "gcs" {
    bucket = "test_gcp_bucket"
    prefix = "terraform/test-0002/test"
    credentials = "/home/test/test-creds.json"
  }
}"""
    assert render == expected_render


def test_google_data_hcl(gbasec):
    render = gbasec.backend.data_hcl("test2")
    expected_render = """data "terraform_remote_state" "test" {
  backend = "gcs"
  config = {
    bucket = "test_gcp_bucket"
    prefix = "terraform/test-0002/test"
    credentials = "/home/test/test-creds.json"
  }
}"""
    assert render == expected_render
