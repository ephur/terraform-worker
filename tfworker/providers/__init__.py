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

from .aws import AWSProvider  # noqa
from .base import UnknownProvider  # noqa
from .generic import GenericProvider  # noqa
from .google import GoogleProvider  # noqa
from .google_beta import GoogleBetaProvider  # noqa
from .helm import HelmProvider  # noqa

ALL = [AWSProvider, GoogleProvider, GoogleBetaProvider, HelmProvider]

REQUIRED_PROVIDERS_TPL = """\
  required_providers {{
{0}
  }}"""


class ProvidersCollection(collections.abc.Mapping):
    def __init__(self, providers_odict, rootc):
        provider_map = dict([(prov.tag, prov) for prov in ALL])
        self._providers = copy.deepcopy(providers_odict)
        for k, v in self._providers.items():
            try:
                self._providers[k] = provider_map[k](v, rootc)

            except KeyError:
                self._providers[k] = GenericProvider(v, tag=k)

    def __len__(self):
        return len(self._providers)

    def __getitem__(self, value):
        if type(value) == int:
            return self._providers[list(self._providers.keys())[value]]
        return self._providers[value]

    def __iter__(self):
        return iter(self._providers.values())

    def hcl(self):
        return "\n".join([prov.hcl() for _, prov in self._providers.items()])

    def required_providers(self):
        content = "\n".join(
            [prov.required() for _, prov in self._providers.items() if prov.source]
        )
        return REQUIRED_PROVIDERS_TPL.format(content)
