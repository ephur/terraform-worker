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

from typing import OrderedDict

from .base import BaseProvider


class HelmProvider(BaseProvider):
    tag = "helm"

    def __init__(self, body, authenticators, **kwargs):
        super(HelmProvider, self).__init__(body)
        self.tag = kwargs.get("tag", self.tag)
        self.vars = body.get("vars", {})
        self.version = body.get("version")

    def hcl(self):
        result = []
        provider_vars = {}
        try:
            for k, v in self.vars.items():
                provider_vars[k] = v
        except (KeyError, TypeError):
            """No provider vars were set."""
            pass

        result.append(f'provider "{self.tag}" {{')
        for k, v in provider_vars.items():

            # Handle special case for kubernetes block in helm provider
            if k.lower() == "kubernetes" and isinstance(v, OrderedDict):
                result.append(f"  {k} {{")
                for ik, iv in v.items():
                    if iv and '"' not in iv:
                        result.append(f'    {ik} = "{iv}"')
                    else:
                        result.append(f"    {ik} = {iv}")
                result.append("  }")
                continue

            if v and '"' not in v:
                result.append(f'  {k} = "{v}"')
            else:
                result.append(f"  {k} = {v}")
        result.append("}")

        return "\n".join(result)
