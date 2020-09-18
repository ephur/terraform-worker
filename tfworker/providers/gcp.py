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
from contextlib import closing
from tfworker.providers import StateError, validate_state_empty
import boto3

import json
import click
"""


class gcp_config(object):
    """
    aws_config provides an object to hold the required configuration needed for AWS
    provider options. This current holds extra attributes in order to expose session
    credentials to terraform.
    """

    def __init__(self):
        pass

    @property
    def key_secret(self):
        pass

    @property
    def key_id(self):
        pass

    @property
    def session_token(self):
        pass

    @property
    def state_bucket(self):
        pass

    @property
    def state_prefix(self):
        pass

    @property
    def region(self):
        pass

    @property
    def state_region(self):
        pass

    @property
    def deployment(self):
        pass

    @property
    def session(self):
        pass

    @property
    def state_session(self):
        pass


def clean_bucket_state(config, definition=None):
    """
    clean_state validates all of the terraform states are empty,
    and then removes the state objects from Cloud Storage

    optionally definition can be passed to limit the cleanup
    to a single definition
    """
    pass


def filter_keys(paginator, bucket_name, prefix="/", delimiter="/", start_after=""):
    """
    filter_keys returns just they keys that are needed
    primarily from: https://stackoverflow.com/questions/30249069/listing-contents-of-a-bucket-with-boto3
    """
    pass


def delete_with_versions(config, key):
    """
    delete_with_versions should handle object deletions, and all references / versions of the object

    note: in initial testing this isn't required, but is inconsistent with how S3 delete markers, and the boto
    delete object call work there may be some configurations that require extra handling.
    """


def get_assumed_role_session(session, role_arn, session_name="AssumedRoleSession1", duration=3600):
    """ get_assumed_role_session returns a boto3 session updated with assumed role credentials """
    pass


def unlock_state(config, definition):
    """
    sometimes terraform doesn't exit cleanly, and can leave
    a particular state locked. This function exists to provide
    an easy mechanism for removing the lock on a particular
    state. Terraform offers this directly, however it's not
    directly accessible when the worker is used to assemble
    terraform configurations.
    """
