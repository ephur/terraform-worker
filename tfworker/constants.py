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


def which(program):
    """ From stack overflow """

    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program):
            return program
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file
    return None


DEFAULT_BACKEND_PREFIX = "terraform/state/{deployment}"
DEFAULT_CONFIG = f"{_CWD}/worker.yaml"
DEFAULT_REPOSITORY_PATH = _CWD
DEFAULT_AWS_REGION = "us-east1"
DEFAULT_GCP_REGION = "us-east1a"
DEFAULT_TERRFORM = which("terraform")

RESERVED_FILES = ["terraform.tf", "worker-locals.tf", "worker.auto.tfvars"]
