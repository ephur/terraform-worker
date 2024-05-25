# Copyright 2021-2023 Richard Maynard (richard.maynard@gmail.com)
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

from unittest import mock

from pkg_resources import DistributionNotFound

from tfworker.commands.version import VersionCommand


def mock_get_distribution(package: str):
    raise DistributionNotFound


class TestVersionCommand:
    def test_exec(self, capsys):
        vc = VersionCommand()
        vc._version = "1.2.3"
        vc.exec()
        text = capsys.readouterr()
        assert text.out == "terraform-worker version 1.2.3\n"
