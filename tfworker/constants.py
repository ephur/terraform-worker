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

DEFAULT_BACKEND_PREFIX = "terraform/state/{deployment}"
DEFAULT_CONFIG = f"{_CWD}/worker.yaml"
DEFAULT_REPOSITORY_PATH = _CWD
DEFAULT_AWS_REGION = "us-east-1"
DEFAULT_GCP_REGION = "us-east-1a"

TF_PROVIDER_DEFAULT_HOSTNAME = "registry.terraform.io"
TF_PROVIDER_DEFAULT_NAMESPACE = "hashicorp"
TF_PROVIDER_DEFAULT_LOCKFILE = ".terraform.lock.hcl"

# Items to refact from CLI / Logging output
REDACTED_ITEMS = ["aws_secret_access_key", "aws_session_token"]

# A map of supported backends, to the authenticators which they require
SUPPORTED_BACKENDS = {"s3": "aws", "gcs": "google"}

TF_STATE_CACHE_NAME = "worker_state_cache.json"
WORKER_LOCALS_FILENAME = "worker_generated_locals.tf"
WORKER_TF_FILENAME = "worker_generated_terraform.tf"
WORKER_TFVARS_FILENAME = "worker_generated.auto.tfvars"
RESERVED_FILES = [
    WORKER_LOCALS_FILENAME,
    WORKER_TF_FILENAME,
    WORKER_TFVARS_FILENAME,
    TF_STATE_CACHE_NAME,
]
