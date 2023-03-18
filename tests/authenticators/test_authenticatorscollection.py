# Copyright 2023 Richard Maynard (richard.maynard@gmail.com)
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

import pytest

import tfworker.authenticators
from tfworker.commands.root import RootCommand


@pytest.fixture
def cli_args(aws_access_key_id, aws_secret_access_key):
    return {
        "aws_access_key_id": aws_access_key_id,
        "aws_secret_access_key": aws_secret_access_key,
        "aws_default_region": "us-east-1",
    }


@pytest.fixture
def state_args(cli_args):
    result = RootCommand.StateArgs()
    for k, v in cli_args.items():
        setattr(result, k, v)
    setattr(result, "backend_bucket", "alphabet")
    return result


class TestAuthenticatorsCollection:
    def test_collection(self, state_args):
        ac = tfworker.authenticators.AuthenticatorsCollection(state_args=state_args)
        assert len(ac) == len(tfworker.authenticators.ALL)

        a0 = ac.get(0)
        assert a0.tag == ac.get(a0.tag).tag

    def test_unknown_authenticator(self, state_args):
        ac = tfworker.authenticators.AuthenticatorsCollection(state_args=state_args)
        assert ac.get("aws") is not None
        with pytest.raises(tfworker.authenticators.UnknownAuthenticator) as e:
            ac.get("unknown")
