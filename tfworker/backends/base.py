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

from abc import ABCMeta, abstractmethod

from tfworker import JSONType


class BackendError(Exception):
    pass


class BaseBackend(metaclass=ABCMeta):
    tag = "base"

    @abstractmethod
    def hcl(self, name: str) -> str:
        pass

    @abstractmethod
    def data_hcl(self, exclude: list) -> str:
        pass

    @abstractmethod
    def clean(self, deployment: str, limit: tuple) -> str:
        pass


class Backends:
    s3 = "s3"
    gcs = "gcs"


def validate_backend_empty(state: JSONType) -> bool:
    """
    validate_backend_empty ensures that the provided state file
    is empty
    """

    try:
        if len(state["resources"]) > 0:
            return False
        else:
            return True
    except KeyError:
        raise BackendError("resources key does not exist in state!")
