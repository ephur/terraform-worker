# Copyright 2020-2023 Richard Maynard (richard.maynard@gmail.com)
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
import json
import os
import shutil
import urllib
import zipfile

import click
from tenacity import (RetryError, retry, retry_if_not_exception_message,
                      stop_after_attempt, wait_chain, wait_fixed)

from tfworker.commands.root import get_platform


class PluginSourceParseException(Exception):
    pass


class PluginsCollection(collections.abc.Mapping):
    def __init__(self, body, temp_dir, cache_dir, tf_version_major):
        self._plugins = body
        self._temp_dir = temp_dir
        self._cache_dir = cache_dir
        self._tf_version_major = tf_version_major

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
        Download the required plugins; or put them in place from the cache dir

        This could be further optimized to not download plugins from hashicorp,
        but rather have them in a local repository or host them in s3, and get
        them from an internal s3 endpoint so no transit charges are incurred.
        Ideally these would be stored between runs, and only downloaded if the
        versions have changed. In production try to remove all all external
        repositories/sources from the critical path.
        """
        opsys, machine = get_platform()
        _platform = f"{opsys}_{machine}"

        plugin_dir = f"{self._temp_dir}/terraform-plugins"

        if not os.path.isdir(plugin_dir):
            os.mkdir(plugin_dir)

        # for each plugin, check if it exists in the cache directory if not download it
        # if it does exist, put it into the rendered terraform plugin cache

        for name, details in self._plugins.items():
            uri = get_url(name, details)
            file_name = uri.split("/")[-1]
            source = PluginSource(name, details)
            provider_path = os.path.join(source.host, source.namespace, name)

            cache_hit = False
            if self._cache_dir is not None:
                cache_hit = check_cache(
                    os.path.join(self._cache_dir, provider_path),
                    os.path.join(plugin_dir, provider_path),
                    name,
                    details["version"],
                    _platform,
                )

            if cache_hit is False:
                # the provider cache is not populated, download and put the plugin in place
                try:
                    download_from_remote(
                        uri,
                        plugin_dir,
                        self._cache_dir,
                        provider_path,
                        file_name,
                        name,
                        details,
                    )
                except PluginSourceParseException as e:
                    click.secho(str(e), fg="red")
                    click.Abort()


class PluginSource:
    """
    Utility object for divining the local module path details from a provider

    Customized source fields are expected in the form: <namespace>/<provider>
    The host can also be specified: <host>/<namespace>/<provider>

    Where the host is NOT specified, registry.terraform.io is assumed.
    """

    def __init__(self, provider, details):
        # Set sensible defaults
        self.provider = provider
        self.namespace = "hashicorp"
        self.host = "registry.terraform.io"
        source = details.get("source")

        # Parse the parts if source defined
        if source:
            items = ["provider", "namespace", "host"]
            parts = source.split("/")
            if len(parts) > 3:
                raise PluginSourceParseException(
                    f"Unable to parse source with more than three segments: {parts}"
                )
            # pop the items in reverse order until there's nothing left
            for item in items:
                if parts:
                    setattr(self, item, parts.pop())

    def __repr__(self):
        return json.dumps(self.__dict__)


@retry(
    wait=wait_chain(
        wait_fixed(2),
        wait_fixed(5),
        wait_fixed(10),
    ),
    stop=stop_after_attempt(3),
    reraise=True,
)
def download_from_remote(
    uri, plugin_dir, cache_dir, provider_path, file_name, name, details
):
    """
    download_and_extract_from_remote handles downloading a plugin from the hashicorp
    provider registry, retries according to the decorator, and optionally places
    downloaded plugins into a local provider cache
    """
    opsys, machine = get_platform()
    _platform = f"{opsys}_{machine}"

    click.secho(
        f"downloading plugin: {name} version {details['version']} from {uri}",
        fg="yellow",
    )

    # download the remote file
    try:
        with urllib.request.urlopen(uri) as response, open(
            f"{plugin_dir}/{file_name}", "wb"
        ) as plug_file:
            shutil.copyfileobj(response, plug_file)
    except urllib.error.HTTPError as e:
        raise PluginSourceParseException(
            f"{e} while downloading plugin: {name}:{details['version']} from {uri}"
        )

    # put the file into the working provider directory and cache if necessary
    files = glob.glob(
        f"{plugin_dir}/terraform-provider*-{name}_{details['version']}*_{_platform}*"
    )
    for afile in files:
        os.chmod(afile, 0o755)
        filename = os.path.basename(afile)
        # handle populating cache
        if cache_dir is not None:
            os.makedirs(os.path.join(cache_dir, provider_path), exist_ok=True)
            shutil.copy(afile, os.path.join(cache_dir, provider_path))
            click.secho(
                f"saved plugin to cache: {name} version {details['version']}",
                fg="yellow",
            )
        os.makedirs(os.path.join(plugin_dir, provider_path), exist_ok=True)
        os.rename(afile, os.path.join(plugin_dir, provider_path, filename))
        click.secho(f"plugin installed to: {plugin_dir}/{provider_path}/", fg="yellow")


def check_cache(cache_path, plugin_path, name, version, platform):
    """
    Determine if the required plugin version already exists in the cache
    """
    # the cache dir doesn't exist, so there's no cache
    if not os.path.exists(cache_path):
        return False

    files = glob.glob(f"{cache_path}/terraform-provider*-{name}_{version}*_{platform}*")
    for afile in files:
        os.makedirs(plugin_path, exist_ok=True)
        shutil.copy(afile, plugin_path)
        click.secho(
            f"using cached provider {name}:{version} from {afile}",
            fg="yellow",
        )
        return True

    # if arrived here, the expected provider package didn't exist
    return False


def get_url(name, details):
    """
    Determine the URL for the plugin

    get URL returns a fully qualifed URL, including the file name.

    In order to support third party terraform plugins we can not
    assume the hashicorp repository. It will function as a default,
    but if baseURL is provided in the plugin settings it will be
    used instead. The logic to determine the complete remote path
    will also be here to simplify the logic in the download method.
    """
    opsys, machine = get_platform()
    _platform = f"{opsys}_{machine}"

    try:
        version = details["version"]
    except KeyError:
        raise KeyError(f"version must be specified for plugin {name}")

    # set the file name, allow it to be overridden with key "filename"
    default_file_name = f"terraform-provider-{name}_{version}_{_platform}.zip"
    file_name = details.get("filename", default_file_name)

    # set the base url, allow it to be overridden with key "baseURL"
    default_base_url = (
        f"https://releases.hashicorp.com/terraform-provider-{name}/{version}"
    )
    base_uri = details.get("baseURL", default_base_url).rstrip("/")

    return f"{base_uri}/{file_name}"
