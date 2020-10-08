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
from contextlib import closing

import click

from .base import BackendError, BaseProvider, validate_backend_empty


class StateError(Exception):
    pass


class AWSProvider(BaseProvider):
    tag = "aws"

    def __init__(self, body, authenticators, *args, **kwargs):
        super(AWSProvider, self).__init__(body)

        self._authenticator = authenticators.get(self.tag)
        self.vars = body.get("vars", {})

    # Provider-specific methods
    def _clean_bucket_state(self, definition=None):
        """
        clean_state validates all of the terraform states are empty,
        and then removes the backend objects from S3

        optionally definition can be passed to limit the cleanup
        to a single definition
        """

        s3_paginator = self._authenticator.backend_session.client("s3").get_paginator(
            "list_objects_v2"
        )
        s3_client = self._authenticator.backend_session.client("s3")
        if definition is None:
            prefix = self._authenticator.prefix
        else:
            prefix = f"{self._authenticator.prefix}/{definition}"

        for s3_object in self.filter_keys(
            s3_paginator, self._authenticator.bucket, prefix
        ):
            backend_file = s3_client.get_object(
                Bucket=self._authenticator.bucket, Key=s3_object
            )
            body = backend_file["Body"]
            with closing(backend_file["Body"]):
                backend = json.load(body)

            if validate_backend_empty(backend):
                self.delete_with_versions(s3_object)
                click.secho(f"backend file removed: {s3_object}", fg="yellow")
            else:
                raise BackendError(f"backend at: {s3_object} is not empty!")

    def _clean_locking_state(self, deployment, definition=None):
        """
        clean_locking_state when called removes the dynamodb table
        that holds all of the state checksums and locking table
        entries
        """
        dynamo_client = self.state_session.resource("dynamodb")
        if definition is None:
            table = dynamo_client.Table(f"terraform-{deployment}")
            table.delete()
        else:
            # delete only the entry for a single state resource
            table = dynamo_client.Table(f"terraform-{deployment}")
            table.delete_item(
                Key={
                    "LockID": f"{self._authenticator.bucket}/{self._authenticator.prefix}/{definition}/terraform.tfstate-md5"
                }
            )

    def clean(self, deployment, limit):
        if limit:
            for limit_item in limit:
                click.secho(
                    "when using limit, dynamodb tables won't be completely dropped",
                    fg="yellow",
                )
                try:
                    # the bucket state deployment is part of the s3 prefix
                    self._clean_bucket_state(definition=limit_item)
                    # deployment name needs specified to determine the dynamo table
                    self._clean_locking_state(deployment, definition=limit_item)
                except StateError as e:
                    click.secho(f"error deleting state: {e}", fg="red")
                    raise SystemExit(1)
        else:
            try:
                self._clean_bucket_state()
            except StateError as e:
                click.secho(f"error deleting state: {e}")
                raise SystemExit(1)
            self._clean_locking_state(deployment)

    def delete_with_versions(self, key):
        """
        delete_with_versions should handle object deletions, and all references / versions of the object

        note: in initial testing this isn't required, but is inconsistent with how S3 delete markers, and the boto
        delete object call work there may be some selfurations that require extra handling.
        """
        s3_client = self.state_session.client("s3")
        s3_client.delete_object(Bucket=self.state_bucket, Key=key)

    @staticmethod
    def filter_keys(paginator, bucket_name, prefix="/", delimiter="/", start_after=""):
        """
        filter_keys returns just they keys that are needed
        primarily from: https://stackoverflow.com/questions/30249069/listing-contents-of-a-bucket-with-boto3
        """

        prefix = prefix[1:] if prefix.startswith(delimiter) else prefix
        start_after = (
            (start_after or prefix) if prefix.endswith(delimiter) else start_after
        )
        try:
            for page in paginator.paginate(
                Bucket=bucket_name, Prefix=prefix, StartAfter=start_after
            ):
                for content in page.get("Contents", ()):
                    yield content["Key"]
        except TypeError:
            pass
