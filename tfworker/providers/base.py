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
        self.config_blocks = body.get("config_blocks", {})
        self.version = self.vars.get("version")
        self.source = body.get("source")

        self._field_filter = ["version"]

    def hcl(self):
        result = []
        provider_vars = {}
        config_block = {}

        # setup data for provider variables and config blocks
        try:
            for k, v in self.vars.items():
                if k not in self._field_filter:
                    provider_vars[k] = v
        except (KeyError, TypeError):
            """No provider vars were set."""
            pass
        for k, v in self.config_blocks.items():
            config_block[k] = v

        # inject provider block
        result.append(f'provider "{self.tag}" {{')

        # inject provider variables
        for k, v in provider_vars.items():
            if v and '"' not in v:
                result.append(f'  {k} = "{v}"')
            else:
                result.append(f"  {k} = {v}")

        # inject provider config blocks
        for k in self.config_blocks.keys():
            result.append(f"  {k} {{")
            result.append(self._hclify(self.config_blocks[k], depth=4))
            result.append("  }")

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

    def _hclify(self, s, depth=4):
        """
        _hcify is a recursive function that takes a string, list or dict
        and turns the results into an HCL compliant string.
        """
        space = ' '
        result = []
        if isinstance(s, str):
            tmps = s.replace('"', "").replace("'", "")
            result.append(f"{space * depth}{s}")
        elif isinstance(s, list):
            tmps = [i.replace('"', "").replace("'", "") for i in s]
            result.append(f"{space * depth}{s}")
        elif isinstance(s, dict):
            # unfortunately, HCL doesn't allow for keys to be quoted so a further
            # check of the value is required to determine how to handle the key
            for k in s.keys():
                if isinstance(s[k], str):
                    result.append(f"{space * depth}{k} = \"{s[k]}\"")
                elif isinstance(s[k], list):
                    result.append(f"{space * depth}{k} = [{s[k]}]")
                elif isinstance(s[k], dict):
                    # decrease depth by 4 to account for extra depth added by hclyifying the key
                    result.append(f"{space * (depth-4)}{self._hclify(k, depth=depth)} = {{")
                    result.append(self._hclify(s[k], depth=depth + 2))
                    result.append(f"{space * depth}}}")
                else:
                    raise TypeError(f"Expected string, list or dict, got {type(s[k])}")
        else:
            raise TypeError(f"Expected string, list or dict, got {type(s)}")

        return "\n".join(result)

class UnknownProvider(Exception):
    def __init__(self, provider):
        super().__init__(f"{provider} is not a known value.")


def validate_backend_region(state):
    """
    validate_backend_region validates that a statefile
    was previously used in the region the current
    deployment is being created for
    """
