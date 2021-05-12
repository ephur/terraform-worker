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


class OptionsCollection(collections.abc.Mapping):
    def __init__(self, options_odict, tf_version_major):
        self._options = options_odict
        self._tf_version_major = tf_version_major

    def __len__(self):
        return len(self._options)

    def __getitem__(self, value):
        if type(value) == int:
            return self._options[list(self._options.keys())[value]]
        return self._options[value]

    def __iter__(self):
        return iter(self._options.values())

    @staticmethod
    def merge(rootc, **kwargs):
        for k, v in rootc.worker_options_odict.items():
            rootc.add_arg(k, v)
        for k, v in kwargs.items():
            rootc.add_arg(k, v)
