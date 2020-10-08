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

import collections
import glob
import os
import shutil
import urllib
import zipfile

import click
from tfworker.commands.root import get_platform


class PluginsCollection(collections.abc.Mapping):
    def __init__(self, body, temp_dir):
        self._plugins = body
        self._temp_dir = temp_dir

    def __len__(self):
        return len(self._providers)

    def __getitem__(self, value):
        if type(value) == int:
            return self._providers[list(self._providers.keys())[value]]
        return self._providers[value]

    def __iter__(self):
        return iter(self._providers.values())

    def download(self):
        """
        Download the required plugins.

        This could be further optimized to not download plugins from hashicorp,
        but rather have them in a local repository or host them in s3, and get
        them from an internal s3 endpoint so no transit charges are incurred.
        Ideally these would be stored between runs, and only downloaded if the
        versions have changed. In production try  to remove all all external
        repositories/sources from the critical path.
        """
        opsys, machine = get_platform()
        _platform = f"{opsys}_{machine}"

        plugin_dir = f"{self._temp_dir}/terraform-plugins"
        if not os.path.isdir(plugin_dir):
            os.mkdir(plugin_dir)

        for name, details in self._plugins.items():
            # Get platform and strip 2 off linux 2.x kernels
            file_name = (
                f"terraform-provider-{name}_{details['version']}_{_platform}.zip"
            )
            uri = f"https://releases.hashicorp.com/terraform-provider-{name}/{details['version']}/{file_name}"
            click.secho(
                f"getting plugin: {name} version {details['version']} from {uri}",
                fg="yellow",
            )

            with urllib.request.urlopen(uri) as response, open(
                f"{plugin_dir}/{file_name}", "wb"
            ) as plug_file:
                shutil.copyfileobj(response, plug_file)
            with zipfile.ZipFile(f"{plugin_dir}/{file_name}") as zip_file:
                zip_file.extractall(f"{plugin_dir}/{_platform}")
            os.remove(f"{plugin_dir}/{file_name}")

            files = glob.glob(f"{plugin_dir}/{_platform}/terraform-provider*")
            for afile in files:
                os.chmod(afile, 0o755)
