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

"""
The providers module contains functions to interact with different cloud providers.
Currently, only AWS is supported, however this provides a model to implement other
providers in the future for things like the remote state store, etc...
"""
from contextlib import closing
from worker.providers import StateError, validate_state_empty
import boto3
import json
import click


class aws_config(object):
    """
    aws_config provides an object to hold the required configuration needed for AWS
    provider options
    """

    def __init__(
        self,
        key_id,
        key_secret,
        region,
        state_bucket,
        state_prefix,
        deployment,
        session_token=None,
    ):
        self.__key_secret = key_secret
        self.__key_id = key_id
        self.__region = region
        self.__state_bucket = state_bucket
        self.__state_prefix = state_prefix
        self.__session_token = session_token
        self.__deployment = deployment

        self.__session = boto3.Session(
            aws_access_key_id=self.__key_id,
            aws_secret_access_key=self.__key_secret,
            aws_session_token=self.__session_token,
            region_name=self.__region,
        )

    @property
    def key_secret(self):
        return self.__key_secret

    @property
    def key_id(self):
        return self.__key_id

    @property
    def session_token(self):
        return self.__session_token

    @property
    def state_bucket(self):
        return self.__state_bucket

    @property
    def state_prefix(self):
        return self.__state_prefix

    @property
    def region(self):
        return self.__region

    @property
    def deployment(self):
        return self.__deployment

    @property
    def session(self):
        return self.__session


def clean_bucket_state(config, definition=None):
    """
    clean_state validates all of the terraform states are empty,
    and then removes the state objects from S3

    optionally definition can be passed to limit the cleanup
    to a single definition
    """

    # get a list of all objects
    # if definition is not none, check if state matches definition
    # validate state is empty
    # remove state file/prefix from S3 (including all previous versions?)
    # s3 = config.session.resource("s3")
    # bucket = s3.Bucket(config.state_bucket)
    # for s3_file in bucket.objects.all():
    #     print(s3_file.key)

    s3_paginator = config.session.client("s3").get_paginator("list_objects_v2")
    s3_client = config.session.client("s3")
    if definition is None:
        prefix = config.state_prefix
    else:
        prefix = "{}/{}".format(config.state_prefix, definition)

    for s3_object in filter_keys(s3_paginator, config.state_bucket, prefix):
        state_file = s3_client.get_object(Bucket=config.state_bucket, Key=s3_object)
        body = state_file["Body"]
        with closing(state_file["Body"]):
            state = json.load(body)

        if validate_state_empty(state):
            delete_with_versions(config, s3_object)
            click.secho("state file removed: {}".format(s3_object), fg="yellow")
        else:
            raise StateError("state at: {} is not empty!".format(s3_object))


def clean_locking_state(config, deployment, definition=None):
    """
    clean_locking_state when called removes the dynamodb table
    that holds all of the state checksums and locking table
    entries
    """
    dynamo_client = config.session.resource("dynamodb")

    if definition is None:
        table = dynamo_client.Table("terraform-{}".format(deployment))
        table.delete()
    else:
        # delete only the entry for a single state resource
        table = dynamo_client.Table("terraform-{}".format(deployment))
        table.delete_item(
            Key={
                "LockID": "{}/{}/{}/terraform.tfstate-md5".format(
                    config.state_bucket, config.state_prefix, definition
                )
            }
        )


def filter_keys(paginator, bucket_name, prefix="/", delimiter="/", start_after=""):
    """
    filter_keys returns just they keys that are needed
    primarily from: https://stackoverflow.com/questions/30249069/listing-contents-of-a-bucket-with-boto3
    """

    prefix = prefix[1:] if prefix.startswith(delimiter) else prefix
    start_after = (start_after or prefix) if prefix.endswith(delimiter) else start_after
    for page in paginator.paginate(
        Bucket=bucket_name, Prefix=prefix, StartAfter=start_after
    ):
        for content in page.get("Contents", ()):
            yield content["Key"]


def delete_with_versions(config, key):
    """
    delete_with_versions should handle object deletions, and all references / versions of the object

    note: in initial testing this isn't required, but is inconsistent with how S3 delete markers, and the boto
    delete object call work there may be some configurations that require extra handling.
    """
    s3_client = config.session.client("s3")
    s3_client.delete_object(Bucket=config.state_bucket, Key=key)


def unlock_state(config, definition):
    """
    sometimes terraform doesn't exit cleanly, and can leave
    a particular state locked. This function exists to provide
    an easy mechanism for removing the lock on a particular
    state. Terraform offers this directly, however it's not
    directly accessible when the worker is used to assemble
    terraform configurations.
    """
