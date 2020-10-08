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

from .base import Backends
from .gcs import GCSBackend  # noqa
from .s3 import S3Backend  # noqa


def select_backend(backend, deployment, authenticators, definitions):
    if backend == Backends.s3:
        return S3Backend(authenticators, definitions, deployment=deployment)
    elif backend == Backends.gcs:
        return GCSBackend(authenticators, definitions, deployment=deployment)
