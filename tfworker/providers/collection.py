import copy
import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Dict, List

from pydantic import GetCoreSchemaHandler, ValidationError
from pydantic_core import CoreSchema, core_schema

import tfworker.util.log as log
from tfworker.exceptions import FrozenInstanceError

if TYPE_CHECKING:
    from tfworker.providers.model import Provider


class ProvidersCollection(Mapping):
    _instance = None
    _lock = threading.Lock()
    _frozen: bool = False

    @classmethod
    def get_named_providers(cls):
        from .google import GoogleProvider
        from .google_beta import GoogleBetaProvider

        NAMED_PROVIDERS = [GoogleProvider, GoogleBetaProvider]
        return NAMED_PROVIDERS

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, providers_odict=None, authenticators: Dict = dict()):
        if not hasattr(self, "_initialized"):
            from .generic import GenericProvider
            from .model import Provider, ProviderConfig

            provider_map = dict(
                [(prov.tag, prov) for prov in ProvidersCollection.get_named_providers()]
            )
            self._providers = copy.deepcopy(providers_odict) if providers_odict else {}
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

                log.trace(f"Adding provider {k} to providers collection")
                self._providers[k] = Provider.model_validate(
                    {"name": k, "obj": obj, "config": config}
                )
                log.trace(
                    f"Provider Attributes: Name:{self._providers[k].name}, GID:{self._providers[k].gid}, Class:{type(self._providers[k].obj)}, Config:{self._providers[k].config}"
                )
            self._initialized = True

    def __len__(self):
        return len(self._providers)

    def __getitem__(self, key: str) -> "Provider":
        return self._providers[key]

    def __iter__(self):
        return iter(self._providers)

    def __str__(self):
        return str([f"{x.name}: {str(x.gid)}" for x in self._providers.values()])

    def __setitem__(self, key, value):
        if self._frozen:
            raise FrozenInstanceError("Cannot modify a frozen instance.")
        self._providers[key] = value

    def freeze(self):
        self._frozen = True

    @classmethod
    def delete_instance(cls):
        cls._instance = None

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, handler(dict))

    def items(self):
        return self._providers.items()

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
            [prov.obj.hcl() for k, prov in self._providers.items() if k in includes]
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
            [
                prov.obj.required()
                for k, prov in self._providers.items()
                if k in includes
            ]
        )
        return_str += "\n  }\n"
        return return_str
