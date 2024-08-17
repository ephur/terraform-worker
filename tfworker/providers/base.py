from typing import TYPE_CHECKING

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

if TYPE_CHECKING:
    from .model import ProviderConfig, ProviderGID


class BaseProvider:
    tag = None
    requires_auth = False

    def __init__(self, config: "ProviderConfig") -> None:
        self.vars = config.vars or {}
        self.config_blocks = config.config_blocks or {}
        self.version = config.requirements.version
        self.source = config.requirements.source or f"hashicorp/{self.tag}"
        self._field_filter = []

    def __str__(self):
        return self.tag

    @property
    def gid(self) -> "ProviderGID":
        from tfworker.util.terraform import get_provider_gid_from_source

        return get_provider_gid_from_source(self.source)

    def hcl(self) -> str:
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

        result.append("}\n")
        return "\n".join(result)

    def required(self):
        return "\n".join(
            [
                f"    {self.tag} = {{",
                f'      source = "{self.source}"',
                f'      version = "{self.version}"',
                "     }\n",
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
        space = " "
        result = []
        if isinstance(s, str):
            result.append(f"{space * depth}{s}")
        elif isinstance(s, list):
            result.append(f"{space * depth}{s}")
        elif isinstance(s, dict):
            # unfortunately, HCL doesn't allow for keys to be quoted so a further
            # check of the value is required to determine how to handle the key
            for k in s.keys():
                if isinstance(s[k], str):
                    result.append(f'{space * depth}{k} = "{s[k]}"')
                elif isinstance(s[k], list):
                    result.append(f"{space * depth}{k} = [{s[k]}]")
                elif isinstance(s[k], dict):
                    # decrease depth by 4 to account for extra depth added by hclyifying the key
                    result.append(
                        f"{space * (depth - 4)}{self._hclify(k, depth=depth)} = {{"
                    )
                    result.append(self._hclify(s[k], depth=depth + 2))
                    result.append(f"{space * depth}}}")
                else:
                    raise TypeError(f"Expected string, list or dict, got {type(s[k])}")
        else:
            raise TypeError(f"Expected string, list or dict, got {type(s)}")

        return "\n".join(result)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        """
        Allow this class to be used as a Pydantic model type
        """
        return core_schema.is_instance_schema(cls)
