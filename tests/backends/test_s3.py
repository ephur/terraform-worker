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
import random
import string

import pytest
from tfworker.backends.base import BackendError

STATE_BUCKET = "test_bucket"
STATE_PREFIX = "terraform"
STATE_REGION = "us-west-2"
STATE_DEPLOYMENT = "test-0001"
EMPTY_STATE = f"{STATE_PREFIX}/{STATE_DEPLOYMENT}/empty/terraform.tfstate"
OCCUPIED_STATE = f"{STATE_PREFIX}/{STATE_DEPLOYMENT}/occupied/terraform.tfstate"
LOCK_DIGEST = "1234123412341234"


@pytest.fixture(scope="class")
def state_setup(request, s3_client, dynamodb_client):
    # location constraint is required due to how we create the client with a specific region
    location = {"LocationConstraint": STATE_REGION}
    s3_client.create_bucket(Bucket=STATE_BUCKET, CreateBucketConfiguration=location)

    with open(
        f"{request.config.rootdir}/tests/fixtures/states/empty.tfstate", "rb"
    ) as f:
        s3_client.put_object(Bucket=STATE_BUCKET, Key=EMPTY_STATE, Body=f)

    with open(
        f"{request.config.rootdir}/tests/fixtures/states/occupied.tfstate", "rb"
    ) as f:
        s3_client.put_object(Bucket=STATE_BUCKET, Key=OCCUPIED_STATE, Body=f)

    # depending on how basec was called/used this may already be created, so don't fail
    # if it already exists
    try:
        dynamodb_client.create_table(
            TableName=f"terraform-{STATE_DEPLOYMENT}",
            KeySchema=[{"AttributeName": "LockID", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "LockID", "AttributeType": "S"},
            ],
            ProvisionedThroughput={
                "ReadCapacityUnits": 1,
                "WriteCapacityUnits": 1,
            },
        )
    except dynamodb_client.exceptions.ResourceInUseException:
        pass

    dynamodb_client.put_item(
        TableName=f"terraform-{STATE_DEPLOYMENT}",
        Item={
            "LockID": {"S": f"{STATE_BUCKET}/{EMPTY_STATE}-md5"},
            "Digest": {"S": f"{LOCK_DIGEST}"},
        },
    )
    dynamodb_client.put_item(
        TableName=f"terraform-{STATE_DEPLOYMENT}",
        Item={
            "LockID": {"S": f"{STATE_BUCKET}/{OCCUPIED_STATE}-md5"},
            "Digest": {"S": f"{LOCK_DIGEST}"},
        },
    )


class TestS3BackendLimit:
    local_test_name = "".join(random.choices(string.ascii_letters, k=10))

    def test_table_creation(self, basec):
        # table should not exist
        assert basec.backend._check_table_exists(self.local_test_name) is False
        # so create it
        assert basec.backend._create_table(self.local_test_name) is None
        # and now it should
        assert basec.backend._check_table_exists(self.local_test_name) is True

    def test_clean_bucket_state(self, basec, state_setup, s3_client):
        # occupied is not empty, so it should raise an error
        with pytest.raises(BackendError):
            basec.backend._clean_bucket_state(definition="occupied")
        # ensure it was not removed
        assert s3_client.get_object(Bucket=STATE_BUCKET, Key=OCCUPIED_STATE)

        # ensure the empty state is present
        assert s3_client.get_object(Bucket=STATE_BUCKET, Key=EMPTY_STATE)
        # this returns nothing
        assert basec.backend._clean_bucket_state(definition="empty") is None
        # but now this should fail
        with pytest.raises(s3_client.exceptions.NoSuchKey):
            s3_client.get_object(Bucket=STATE_BUCKET, Key=EMPTY_STATE)

    def test_clean_locking_state(self, basec, state_setup, dynamodb_client):
        # validate items exist before function call
        resp = dynamodb_client.get_item(
            TableName=f"terraform-{STATE_DEPLOYMENT}",
            Key={"LockID": {"S": f"{STATE_BUCKET}/{EMPTY_STATE}-md5"}},
        )
        assert resp["Item"]["Digest"] == {"S": LOCK_DIGEST}
        resp = dynamodb_client.get_item(
            TableName=f"terraform-{STATE_DEPLOYMENT}",
            Key={"LockID": {"S": f"{STATE_BUCKET}/{OCCUPIED_STATE}-md5"}},
        )
        assert resp["Item"]["Digest"] == {"S": LOCK_DIGEST}

        # this should remove just empty
        assert basec.backend._clean_locking_state(STATE_DEPLOYMENT, "empty") is None

        # validate empty is gone, and occupied is not
        resp = dynamodb_client.get_item(
            TableName=f"terraform-{STATE_DEPLOYMENT}",
            Key={"LockID": {"S": f"{STATE_BUCKET}/{EMPTY_STATE}-md5"}},
        )
        with pytest.raises(KeyError):
            assert resp["Item"]
        resp = dynamodb_client.get_item(
            TableName=f"terraform-{STATE_DEPLOYMENT}",
            Key={"LockID": {"S": f"{STATE_BUCKET}/{OCCUPIED_STATE}-md5"}},
        )
        assert resp["Item"]["Digest"] == {"S": LOCK_DIGEST}


class TestS3BackendAll:
    def test_clean_bucket_state(self, request, basec, state_setup, s3_client):
        # need to patch the occupied object with the empty one
        with open(
            f"{request.config.rootdir}/tests/fixtures/states/empty.tfstate", "rb"
        ) as f:
            s3_client.put_object(Bucket=STATE_BUCKET, Key=OCCUPIED_STATE, Body=f)
        assert basec.backend._clean_bucket_state() is None

    def test_clean_locking_state(self, basec, state_setup, dynamodb_client):
        # ensure the table exists before test, then remove it, and make sure it's gone
        assert (
            f"terraform-{STATE_DEPLOYMENT}"
            in dynamodb_client.list_tables()["TableNames"]
        )
        assert basec.backend._clean_locking_state(STATE_DEPLOYMENT) is None
        assert (
            f"terraform-{STATE_DEPLOYMENT}"
            not in dynamodb_client.list_tables()["TableNames"]
        )


def test_backend_clean_all(basec, request, state_setup, dynamodb_client, s3_client):
    # this function should trigger an exit
    with pytest.raises(SystemExit):
        basec.backend.clean(STATE_DEPLOYMENT)

    # empty the occupied state
    with open(
        f"{request.config.rootdir}/tests/fixtures/states/empty.tfstate", "rb"
    ) as f:
        s3_client.put_object(Bucket=STATE_BUCKET, Key=OCCUPIED_STATE, Body=f)

    # now clean should run and return nothing
    assert basec.backend.clean(STATE_DEPLOYMENT) is None


def test_backend_clean_limit(basec, request, state_setup, dynamodb_client, s3_client):
    with pytest.raises(SystemExit):
        basec.backend.clean(STATE_DEPLOYMENT, limit=["occupied"])
    assert basec.backend.clean(STATE_DEPLOYMENT, limit=["empty"]) is None


def test_s3_hcl(basec):
    render = basec.backend.hcl("test")
    expected_render = """  backend "s3" {
    region = "us-west-2"
    bucket = "test_bucket"
    key = "terraform/test-0001/test/terraform.tfstate"
    dynamodb_table = "terraform-test-0001"
    encrypt = "true"
  }"""
    assert render == expected_render


def test_s3_data_hcl(basec):
    expected_render = """data "terraform_remote_state" "test" {
  backend = "s3"
  config = {
    region = "us-west-2"
    bucket = "test_bucket"
    key = "terraform/test-0001/test/terraform.tfstate"
  }
}
"""
    render = []
    render.append(basec.backend.data_hcl(["test", "test"]))
    render.append(basec.backend.data_hcl(["test"]))
    for i in render:
        assert i == expected_render

    with pytest.raises(ValueError):
        render.append(basec.backend.data_hcl("test"))
