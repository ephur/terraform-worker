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
    # add custom "help" parameter to the exception
    def __init__(self, message, help=None):
        super().__init__(message)
        self._help = help

    @property
    def help(self):
        if self._help is None:
            return "No help available"
        return self._help


class BaseBackend(metaclass=ABCMeta):
    plan_storage = False
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

    @abstractmethod
    def remotes(self) -> list:
        pass

    @property
    def handlers(self) -> dict:
        return {}


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
