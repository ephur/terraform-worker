import copy
from collections.abc import Mapping
from typing import Dict, List

from pydantic import GetCoreSchemaHandler, ValidationError
from pydantic_core import CoreSchema, core_schema

from tfworker.providers.generic import GenericProvider
from tfworker.providers.google import GoogleProvider
from tfworker.providers.google_beta import GoogleBetaProvider

# from tfworker.types.provider import ProviderConfig, Provider

NAMED_PROVIDERS = [GoogleProvider, GoogleBetaProvider]


class ProvidersCollection(Mapping):
    def __init__(self, providers_odict, authenticators: Dict = dict()):
        from tfworker.types.provider import Provider, ProviderConfig

        provider_map = dict([(prov.tag, prov) for prov in NAMED_PROVIDERS])
        self._providers = copy.deepcopy(providers_odict)
        for k, v in self._providers.items():
            try:
                config = ProviderConfig.model_validate(v)
            except ValidationError as e:
                e.ctx = ("provider", k)
                raise e

            if k in provider_map:
                obj = provider_map[k](config)
            else:
                obj = GenericProvider(config, tag=k)

            if obj.requires_auth:
                obj.add_authenticators(authenticators)

            self._providers[k] = Provider(name=k, obj=obj, config=config)

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

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, handler(dict))

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
