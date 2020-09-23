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

import os

_CWD = os.getcwd()

DEFAULT_BACKEND_BUCKET = "tfworker-terraform-states"
DEFAULT_BACKEND_PREFIX = "terraform/state/{deployment}"
DEFAULT_CONFIG = f"{_CWD}/worker.yaml"
DEFAULT_REPOSITORY_PATH = _CWD
DEFAULT_AWS_REGION = "us-west-2"
DEFAULT_GCP_REGION = "us-west2b"
DEFAULT_BACKEND_REGION = "us-west-2"
DEFAULT_TERRFORM = "/usr/local/bin/terraform"

RESERVED_FILES = ["terraform.tf", "worker-locals.tf"]
