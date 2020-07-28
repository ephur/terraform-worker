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


class ProviderError(Exception):
    pass


class StateError(Exception):
    pass


def validate_state_empty(state):
    """
    validate_empty_state ensures that the provided state file
    is empty
    """

    try:
        if len(state["resources"]) > 0:
            return False
        else:
            return True
    except KeyError:
        raise StateError("resources key does not exist in state!")


def validate_state_region(state):
    """
    validate_state_region validates that a statefile
    was previously used in the region the current
    deployment is being created for
    """
