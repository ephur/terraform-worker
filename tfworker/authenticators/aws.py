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

import shlex

import boto3
from tfworker import constants as const

from .base import BaseAuthenticator


class AWSAuthenticator(BaseAuthenticator):
    tag = "aws"

    def __init__(self, state_args, **kwargs):
        super(AWSAuthenticator, self).__init__(state_args, **kwargs)

        self.access_key_id = self._resolve_arg("aws_access_key_id")
        self.backend_region = self._resolve_arg("backend_region")
        self.bucket = self._resolve_arg("backend_bucket")
        self.prefix = self._resolve_arg("backend_prefix")
        self.profile = self._resolve_arg("aws_profile")
        self.region = self._resolve_arg("aws_region")
        self.role_arn = self._resolve_arg("role_arn")
        self.secret_access_key = self._resolve_arg("aws_secret_access_key")
        self.session_token = self._resolve_arg("aws_session_token")

        self.deployment = kwargs.get("deployment")

        self._account_id = None
        self._backend_session = None
        self._session = None

        # If the default value is used, render the deployment name into it
        if self.prefix == const.DEFAULT_BACKEND_PREFIX:
            self.prefix = const.DEFAULT_BACKEND_PREFIX.format(
                deployment=self.deployment
            )

        aws_is_active = (self.access_key_id and self.secret_access_key) or self.profile
        if aws_is_active:
            # Initialize the session objects
            self._session = boto3.Session(
                region_name=self.region, **self._session_state_args
            )

            if not self.role_arn:
                # if a role was not provided, need to ensure credentials areset
                # in the config, these will come from the session
                self.access_key_id = self._session.get_credentials().access_key
                self.secret_access_key = self._session.get_credentials().secret_key
                self.session_token = self._session.get_credentials().token

                if self.backend_region == self.region:
                    self._backend_session = self._session
                else:
                    self._backend_session = boto3.Session(
                        region_name=self.backend_region, **self._session_state_args
                    )
            else:
                (self.__session, creds) = AWSAuthenticator.get_assumed_role_session(
                    self._session, self.self.role_arn
                )
                self.access_key_id = creds["AccessKeyId"]
                self.secret_access_key = creds["SecretAccessKey"]
                self.session_token = creds["SessionToken"]

                if self.backend_region == self.region:
                    self._backend_session = self._session
                else:
                    self._backend_session = boto3.Session(
                        region_name=self.backend_region,
                        aws_access_key_id=self.access_key_id,
                        aws_secret_access_key=self.secret_access_key,
                        aws_session_token=self.session_token,
                    )

    @property
    def _session_state_args(self):
        state_args = dict()

        if self.profile:
            state_args["profile_name"] = self.profile

        if self.access_key_id:
            state_args["aws_access_key_id"] = self.access_key_id

        if self.secret_access_key:
            state_args["aws_secret_access_key"] = self.secret_access_key

        if self.session_token is not None:
            state_args["aws_session_token"] = self.session_token

        return state_args

    @property
    def account_id(self):
        if not self._account_id:
            self._account_id = AWSAuthenticator.get_aws_id(
                self.access_key_id,
                self.secret_access_key,
                self.session_token,
            )
        return self._account_id

    @property
    def backend_session(self):
        return self._backend_session

    @property
    def session(self):
        return self._session

    def env(self):
        result = {}
        if self.access_key_id:
            result["AWS_ACCESS_KEY_ID"] = shlex.quote(self.access_key_id)
        if self.region:
            result["AWS_DEFAULT_REGION"] = shlex.quote(self.region)
        if self.secret_access_key:
            result["AWS_SECRET_ACCESS_KEY"] = shlex.quote(self.secret_access_key)
        if self.session_token:
            result["AWS_SESSION_TOKEN"] = shlex.quote(self.session_token)
        return result

    @staticmethod
    def get_assumed_role_session(
        session, role_arn, session_name="AssumedRoleSession1", duration=3600
    ):
        """ get_assumed_role_session returns a boto3 session updated with assumed role credentials """
        sts_client = session.client("sts")
        role_creds = sts_client.assume_role(
            RoleArn=role_arn, RoleSessionName=session_name, DurationSeconds=duration
        )["Credentials"]

        new_session = boto3.Session(
            aws_access_key_id=role_creds["AccessKeyId"],
            aws_secret_access_key=role_creds["SecretAccessKey"],
            aws_session_token=role_creds["SessionToken"],
        )

        return new_session, role_creds

    @staticmethod
    def get_aws_id(key_id, key_secret, session_token=None):
        """Return the AWS account ID."""
        client = boto3.client(
            "sts",
            aws_access_key_id=key_id,
            aws_secret_access_key=key_secret,
            aws_session_token=session_token,
        )
        return client.get_caller_identity()["Account"]
