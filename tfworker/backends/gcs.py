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

import json

import click
from google.api_core import page_iterator
from google.cloud import storage
from google.cloud.exceptions import Conflict

from .base import BackendError, BaseBackend, validate_backend_empty


class GCSBackend(BaseBackend):
    tag = "gcs"
    auth_tag = "google"

    def __init__(self, authenticators, definitions, deployment=None):
        self._authenticator = authenticators[self.auth_tag]
        self._definitions = definitions
        self._gcs_bucket = None
        self._gcs_prefix = None

        if deployment:
            self._deployment = deployment
            self._gcs_bucket = self._authenticator.bucket
            self._gcs_prefix = self._authenticator.prefix
            if not self._gcs_prefix.endswith("/"):
                self._gcs_prefix = f"{self._gcs_prefix}/"

            if self._authenticator.creds_path:
                self._storage_client = storage.Client.from_service_account_json(
                    self._authenticator.creds_path
                )
            else:
                self._storage_client = storage.Client(
                    project=self._authenticator.project
                )

            try:
                self._storage_client.create_bucket(self._gcs_bucket)
            except Conflict:
                pass

    def _clean_deployment_limit(self, limit: tuple) -> None:
        """ only clean items within limit """
        full_state_list = self._get_state_list()

        # first validate all items in limit are present before removing anything
        for item in limit:
            if item not in full_state_list:
                raise BackendError(
                    f"limit item {item} not found in state list [{','.join(full_state_list)}]"
                )

        # validation completed, do cleaning
        for item in limit:
            self._clean_prefix(f"{self._gcs_prefix}/{item}")

    def _clean_prefix(self, prefix: str) -> None:
        bucket = self._storage_client.get_bucket(self._gcs_bucket)
        blobs = bucket.list_blobs(prefix=prefix)
        for b in blobs:
            name = b.name.split("/")[-1]
            # check specifically for a locking operation to indicate failure condition
            if name == "default.tflock":
                raise BackendError(f"state lock found at {b.name}")
            # fail if there are any other files in the bucket
            if name != "default.tfstate":
                raise BackendError(f"unexpected item found in state bucket: {b.name}")

            state = json.loads(b.download_as_string().decode("utf-8"))
            if validate_backend_empty(state):
                b.delete()
                click.secho(f"empty state file {b.name} removed", fg="green")
            else:
                raise BackendError(f"state file at: {b.name} is not empty")

    def _get_state_list(self) -> list:
        """
        _get_state_list returns a list of states inside of the prefix, since this is looking for state/folders only
        it is not possible with a native method from the SDK
        """

        gcs_params = {
            "projection": "noAcl",
            "prefix": self._gcs_prefix,
            "delimiter": "/",
        }
        iter_path = f"/b/{self._gcs_bucket}/o"

        iterator = page_iterator.HTTPIterator(
            client=self._storage_client,
            api_request=self._storage_client._connection.api_request,
            path=iter_path,
            items_key="prefixes",
            item_to_value=self._item_to_value,
            extra_params=gcs_params,
        )

        return [self._parse_gcs_items(x) for x in iterator]

    @staticmethod
    def _item_to_value(_, item: str) -> str:
        """
        _item_to_value is required to format the item, logic here
        is eliminated since the scope where this function is called is
        deep within the gcloud SDK
        """
        return item

    def _parse_gcs_items(self, item: str) -> str:
        """
        _parse_gcs_items ensures all the items retrieved are expected path keys,
        matching the deployment, and are formatted correctly, finally the
        definition name is extracted from the path and returned
        """
        if (not item.startswith(self._gcs_prefix)) or (not item.endswith("/")):
            raise BackendError(f"unexpected path returned from GCS: {item}")
        return item.split("/")[-2]

    def clean(self, deployment: str, limit: tuple = None) -> None:
        """
        clean is the top level clean method that handles determining what states to clean/remove
        from gcs, and calling the proper methods to remove at the desired scope
        """
        if self._gcs_prefix is None or self._gcs_bucket is None:
            raise BackendError("clean attempted without proper authenticator setup")

        # clean entire deployment
        if not limit:
            self._clean_prefix(self._gcs_prefix)
            return

        # only clean specified limit
        self._clean_deployment_limit(limit)

    def hcl(self, name: str) -> str:
        state_config = []
        state_config.append('  backend "gcs" {')
        state_config.append(f'    bucket = "{self._authenticator.bucket}"')
        state_config.append(f'    prefix = "{self._authenticator.prefix}/{name}"')
        if self._authenticator.creds_path:
            state_config.append(f'    credentials = "{self._authenticator.creds_path}"')
        state_config.append("  }")
        return "\n".join(state_config)

    def data_hcl(self, remotes: list) -> str:
        remote_data_config = []
        if type(remotes) is not list:
            raise ValueError("remotes must be a list")

        for remote in set(remotes):
            remote_data_config.append(f'data "terraform_remote_state" "{remote}" {{')
            remote_data_config.append('  backend = "gcs"')
            remote_data_config.append("  config = {")
            remote_data_config.append(f'    bucket = "{self._authenticator.bucket}"')
            remote_data_config.append(
                f'    prefix = "{self._authenticator.prefix}/{remote}"'
            )
            if self._authenticator.creds_path:
                remote_data_config.append(
                    f'    credentials = "{self._authenticator.creds_path}"'
                )
            remote_data_config.append("  }")
            remote_data_config.append("}")
        return "\n".join(remote_data_config)
