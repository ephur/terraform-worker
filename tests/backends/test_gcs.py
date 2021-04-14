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

from unittest.mock import patch

import pytest
from tfworker.backends.base import BackendError


class MockGCSClient:
    def __init__(self):
        self._connection = MockGCSConnection()

    @property
    def get_bucket(self):
        return MockGCSBucket


class MockGCSConnection:
    @property
    def api_request(self):
        return None


class MockGCSBucket:
    def __init__(self, bucket):
        self._blobs = [
            MockGCSBlob("terraform/test/foo/default.tfstate", "", b'{"resources":[]}'),
            MockGCSBlob("terraform/test/bar/default.tfstate", "", b'{"resources":[]}'),
            MockGCSBlob(
                "terraform/fail/tflock/default.tflock", "", b'{"resources":[]}'
            ),
            MockGCSBlob("terraform/fail/other/other.file", "", b'{"resources":[]}'),
        ]

        with open(f"{bucket}/tests/fixtures/states/occupied.tfstate", "rb") as f:
            self._blobs.append(
                MockGCSBlob("terraform/fail/occupied/default.tfstate", "", f.read())
            )

    def list_blobs(self, prefix):
        blobs = list(filter(lambda x: x.name.startswith(prefix), self._blobs))
        assert len(blobs) > 0
        return blobs


class MockGCSBlob:
    def __init__(self, name, path, content):
        self.name = name
        self.path = path
        self.content = content

    def download_as_string(self):
        return self.content

    def delete(self):
        pass


class TestClean:
    def test_clean_prefix_check(self, gbasec):
        gbasec.backend._gcs_prefix = None
        gbasec.backend._gcs_bucket = None
        with pytest.raises(BackendError):
            gbasec.backend.clean("test")

        gbasec.backend._gcs_prefix = "valid"
        gbasec.backend._gcs_bucket = None
        with pytest.raises(BackendError):
            gbasec.backend.clean("test")

        gbasec.backend._gcs_prefix = None
        gbasec.backend._gcs_bucket = "valid"
        with pytest.raises(BackendError):
            gbasec.backend.clean("test")

    @patch("tfworker.backends.GCSBackend._clean_deployment_limit")
    def test_clean_limit_check(self, mock_clean, gbasec):
        mock_clean.return_value = None
        gbasec.backend._storage_client = MockGCSClient()
        assert gbasec.backend.clean("test", limit=("test-limit",)) is None
        mock_clean.assert_called_once_with(("test-limit",))

    @patch("tfworker.backends.GCSBackend._clean_prefix")
    def test_clean_no_limit_check(self, mock_clean, gbasec):
        mock_clean.return_value = None
        gbasec.backend._storage_client = MockGCSClient()
        gbasec.backend._gcs_prefix = "test-prefix"
        assert gbasec.backend.clean("test") is None
        mock_clean.assert_called_once_with("test-prefix")

    @patch("google.api_core.page_iterator.HTTPIterator")
    @patch("tfworker.backends.GCSBackend._clean_prefix")
    def test_clean_deployment_limit(self, mock_clean, mock_iter, gbasec):
        mock_iter.return_value = ["terraform/test/foo/", "terraform/test/bar/"]
        mock_clean.return_value = None
        gbasec.backend._storage_client = MockGCSClient()
        gbasec.backend._gcs_prefix = "terraform/test"

        with pytest.raises(BackendError):
            gbasec.backend._clean_deployment_limit(("zed",))

        gbasec.backend._clean_deployment_limit(
            (
                "foo",
                "bar",
            )
        )
        assert mock_clean.call_count == 2

    @patch("google.api_core.page_iterator.HTTPIterator")
    def test_clean_prefix(self, mock_iter, gbasec, request):
        mock_iter.return_value = ["terraform/test/foo/", "terraform/test/bar/"]
        gbasec.backend._storage_client = MockGCSClient()
        gbasec.backend._gcs_bucket = request.config.rootdir

        assert gbasec.backend._clean_prefix("terraform/test") is None

        with pytest.raises(BackendError):
            gbasec.backend._clean_prefix("terraform/fail/tflock")

        with pytest.raises(BackendError):
            gbasec.backend._clean_prefix("terraform/fail/other")

        with pytest.raises(BackendError):
            gbasec.backend._clean_prefix("terraform/fail/occupied")

    @patch("google.api_core.page_iterator.HTTPIterator")
    def test_get_state_list(self, mock_iter, gbasec):
        mock_iter.return_value = ["foo/", "bar/"]
        gbasec.backend._storage_client = MockGCSClient()
        gbasec.backend._gcs_prefix = ""
        items = gbasec.backend._get_state_list()
        assert items == ["foo", "bar"]

        mock_iter.return_value = ["terraform/test/foo/", "terraform/test/bar/"]
        gbasec.backend._gcs_prefix = "terraform/test/"
        items = gbasec.backend._get_state_list()
        assert items == ["foo", "bar"]

    def test_item_to_value(self, gbasec):
        assert gbasec.backend._item_to_value("", "foo") == "foo"

    @pytest.mark.parametrize(
        "prefix, inval, outval, expected_raise",
        [
            ("terraform", "terraform/foo/", "foo", None),
            ("terraform/a/b/c", "terraform/a/b/c/foo/", "foo", None),
            ("terraform/a/b/c", "terraform/a/b/c/foo", "", BackendError),
            ("terraform", "junk", "", BackendError),
        ],
    )
    def test_parse_gcs_items(self, gbasec, prefix, inval, outval, expected_raise):
        gbasec.backend._gcs_prefix = prefix
        if expected_raise:
            with pytest.raises(expected_raise):
                gbasec.backend._parse_gcs_items(inval)
        else:
            assert gbasec.backend._parse_gcs_items(inval) == outval


def test_google_hcl(gbasec):
    render = gbasec.backend.hcl("test")
    expected_render = """  backend "gcs" {
    bucket = "test_gcp_bucket"
    prefix = "terraform/test-0002/test"
    credentials = "/home/test/test-creds.json"
  }"""
    assert render == expected_render


def test_google_data_hcl(gbasec):
    expected_render = """data "terraform_remote_state" "test" {
  backend = "gcs"
  config = {
    bucket = "test_gcp_bucket"
    prefix = "terraform/test-0002/test"
    credentials = "/home/test/test-creds.json"
  }
}"""
    render = []
    render.append(gbasec.backend.data_hcl(["test", "test"]))
    render.append(gbasec.backend.data_hcl(["test"]))
    for i in render:
        assert i == expected_render

    with pytest.raises(ValueError):
        render.append(gbasec.backend.data_hcl("test"))
