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

from .base import BaseProvider


class AWSProvider(BaseProvider):
    tag = "aws"

    def __init__(self, body, authenticators, tf_version_major, **kwargs):
        super(AWSProvider, self).__init__(body, tf_version_major)
        self._authenticator = authenticators.get(self.tag)
        # need to refresh vars after getting the authenticator as vars
        # could include authentication information
        self.vars = body.get("vars", {})
