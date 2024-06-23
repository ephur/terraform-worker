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

import collections
import copy
from typing import List

from tfworker.providers.generic import GenericProvider
from tfworker.providers.google import GoogleProvider
from tfworker.providers.google_beta import GoogleBetaProvider
from tfworker.types.provider import ProviderConfig

NAMED_PROVIDERS = [GoogleProvider, GoogleBetaProvider]


class ProvidersCollection(collections.abc.Mapping):
    def __init__(self, providers_odict, authenticators):
        provider_map = dict([(prov.tag, prov) for prov in NAMED_PROVIDERS])
        self._providers = copy.deepcopy(providers_odict)
        for k, v in self._providers.items():
            config = ProviderConfig.model_validate(v)

            if k in provider_map:
                self._providers[k] = provider_map[k](config)
                if self._providers[k].requires_auth:
                    self._providers[k].add_authenticators(authenticators)
            else:
                self._providers[k] = GenericProvider(config, tag=k)

    def __len__(self):
        return len(self._providers)

    def __getitem__(self, value):
        if type(value) is int:
            return self._providers[list(self._providers.keys())[value]]
        return self._providers[value]

    def __iter__(self):
        return iter(self._providers.values())

    def __str__(self):
        return str([f"{x.tag}: {str(x.gid)}" for x in self._providers.values()])

    def keys(self):
        return self._providers.keys()

    def provider_hcl(self, includes: List[str] = None) -> str:
        """
        Returns a string of HCL code for the specified providers.

        If no providers are specified, HCL code for all providers is returned.

        Args:
            includes (List[str], optional): List of provider keys to include.
                                            Defaults to None, which includes all providers.

        Returns:
            str: HCL code for the specified providers.
        """
        if includes is None:
            includes = list(self._providers.keys())

        return "\n".join(
            [prov.hcl() for k, prov in self._providers.items() if k in includes]
        )

    def required_hcl(self, includes: List[str] = None) -> str:
        """
        Returns a string of HCL code for the terraform "required" block for the specified providers.

        If no providers are specified, HCL code for all providers is returned.

        Args:
            includes (List[str], optional): List of provider keys to include.
                                            Defaults to None, which includes all providers.

        Returns:
            str: HCL code for the specified providers.
        """
        if includes is None:
            includes = list(self._providers.keys())

        return_str = "  required_providers {\n"
        return_str += "\n".join(
            [prov.required() for k, prov in self._providers.items() if k in includes]
        )
        return_str += "\n  }"
        return return_str
