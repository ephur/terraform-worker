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

from tfworker.commands.version import VersionCommand


def test_version_command(capsys):
    with mock.patch("tfworker.commands.version.get_version") as mock_get_version:
        mock_get_version.return_value = "1.2.3"
        command = VersionCommand()
        command.exec()
        text = capsys.readouterr()
        assert text.out == "terraform-worker version 1.2.3\n"
