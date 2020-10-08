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

import glob
import os

import tfworker.commands.root
import tfworker.plugins


class TestPlugins:
    def test_plugin_download(self, rootc):
        opsys, machine = tfworker.commands.root.get_platform()
        plugins = tfworker.plugins.PluginsCollection(
            {"null": {"version": "2.1.2"}}, rootc.temp_dir
        )
        plugins.download()
        files = glob.glob(
            f"{rootc.temp_dir}/terraform-plugins/{opsys}_{machine}/terraform-provider-null_v2.1.2*"
        )
        assert len(files) > 0
        for afile in files:
            assert os.path.isfile(afile)
            assert (os.stat(afile).st_mode & 0o777) == 0o755
