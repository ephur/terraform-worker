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

import glob
import os
from contextlib import contextmanager

import pytest

import tfworker.commands.root
import tfworker.plugins

# values needed by multiple tests
opsys, machine = tfworker.commands.root.get_platform()
_platform = f"{opsys}_{machine}"


# context manager to allow testing exceptions in parameterized tests
@contextmanager
def does_not_raise():
    yield


# test data to ensure URL's are formed correctly, and exception is thrown
# when version is not passed
test_get_url_data = [
    (
        "default_test",
        {"version": "1.0.0"},
        f"https://releases.hashicorp.com/terraform-provider-default_test/1.0.0/terraform-provider-default_test_1.0.0_{_platform}.zip",
        does_not_raise(),
    ),
    (
        "uri_test",
        {"version": "1.5.0", "baseURL": "http://localhost/"},
        f"http://localhost/terraform-provider-uri_test_1.5.0_{_platform}.zip",
        does_not_raise(),
    ),
    (
        "filename_test",
        {"version": "2.0.0", "filename": "filename_test.zip"},
        "https://releases.hashicorp.com/terraform-provider-filename_test/2.0.0/filename_test.zip",
        does_not_raise(),
    ),
    (
        "filename_and_uri_test",
        {
            "version": "2.5.0",
            "filename": "filename_test.zip",
            "baseURL": "http://localhost/",
        },
        "http://localhost/filename_test.zip",
        does_not_raise(),
    ),
    ("bad_version", {}, None, pytest.raises(KeyError)),
]


class TestPlugins:
    @pytest.mark.enable_socket
    @pytest.mark.depends(on="get_url")
    def test_plugin_download(self, rootc):
        plugins = tfworker.plugins.PluginsCollection(
            {"null": {"version": "3.2.1"}}, rootc.temp_dir, None, 1
        )
        plugins.download()
        files = glob.glob(
            f"{rootc.temp_dir}/terraform-plugins/registry.terraform.io/hashicorp/null/*null*3.2.1*.zip"
        )
        assert len(files) == 1
        for afile in files:
            assert os.path.isfile(afile)
            assert (os.stat(afile).st_mode & 0o777) == 0o755

    @pytest.mark.depends(name="get_url")
    @pytest.mark.parametrize(
        "name,details,expected_url, expected_exception", test_get_url_data
    )
    def test_get_url(self, name, details, expected_url, expected_exception):
        with expected_exception:
            actual_url = tfworker.plugins.get_url(name, details)
            assert expected_url == actual_url

    @pytest.mark.parametrize(
        "name,details,expected_host,expected_ns,expected_provider,expected_exception",
        [
            (
                "bar",
                {"source": "foo/bar"},
                "registry.terraform.io",
                "foo",
                "bar",
                does_not_raise(),
            ),
            (
                "bar",
                {"source": "gh.com/foo/bar"},
                "gh.com",
                "foo",
                "bar",
                does_not_raise(),
            ),
            (
                "bar",
                {"source": "bar"},
                "registry.terraform.io",
                "hashicorp",
                "bar",
                does_not_raise(),
            ),
            (
                "bar",
                {},
                "registry.terraform.io",
                "hashicorp",
                "bar",
                does_not_raise(),
            ),
            (
                "bar",
                {"source": "gh.com/extra/foo/bar"},
                "registry.terraform.io",
                "hashicorp",
                "bar",
                pytest.raises(tfworker.plugins.PluginSourceParseException),
            ),
        ],
    )
    def test_plugin_source(
        self,
        name,
        details,
        expected_host,
        expected_ns,
        expected_provider,
        expected_exception,
    ):
        with expected_exception:
            source = tfworker.plugins.PluginSource(name, details)
            assert source.host == expected_host
            assert source.namespace == expected_ns
            assert source.provider == expected_provider
