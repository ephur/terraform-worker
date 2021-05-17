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


class BaseProvider:
    tag = None

    def __init__(self, body, tf_version_major):
        self._tf_version_major = tf_version_major

        self.vars = body.get("vars", {})
        self.version = self.vars.get("version")
        self.source = body.get("source")

        self._field_filter = ["version"]

    def hcl(self):
        result = []
        provider_vars = {}
        try:
            for k, v in self.vars.items():
                if self._tf_version_major >= 13:
                    if k not in self._field_filter:
                        provider_vars[k] = v
                else:
                    provider_vars[k] = v
        except (KeyError, TypeError):
            """No provider vars were set."""
            pass

        result.append(f'provider "{self.tag}" {{')
        for k, v in provider_vars.items():
            if v and '"' not in v:
                result.append(f'  {k} = "{v}"')
            else:
                result.append(f"  {k} = {v}")
        result.append("}")
        return "\n".join(result)

    def required(self):
        return "\n".join(
            [
                f"    {self.tag} = {{",
                f'      source = "{self.source}"',
                f'      version = "{self.version}"',
                "     }",
            ]
        )

    def clean(self, deployment, limit, config):
        """Nothing to do here so far"""
        pass


class UnknownProvider(Exception):
    def __init__(self, provider):
        super().__init__(f"{provider} is not a known value.")


def validate_backend_region(state):
    """
    validate_backend_region validates that a statefile
    was previously used in the region the current
    deployment is being created for
    """
