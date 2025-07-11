import threading
from collections.abc import Mapping
from typing import Dict, List

from pydantic import GetCoreSchemaHandler, ValidationError
from pydantic_core import CoreSchema, core_schema

import tfworker.util.log as log
from tfworker.exceptions import FrozenInstanceError
from tfworker.util.cli import handle_config_error

from .model import Definition


class DefinitionsCollection(Mapping):
    """
    The DefinitionsCollection holds information about all of the definitions that will need
    to be managed during the execution for a particular deployment. The collection should be
    used to pass resources to independent functions rather than containing all of the logic.
    """

    _instance = None
    _lock = threading.Lock()
    _frozen: bool = False

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self, definitions: Dict[str, "Definition"], limiter: List[str] | None = None
    ) -> None:
        if not hasattr(self, "_initialized"):
            log.trace("initializing DefinitionsCollection")
            self._definitions = {}
            if limiter is None:
                limiter = []
            for definition, body in definitions.items():
                # disallow commas in definition names
                if "," in definition:
                    raise ValueError(
                        f"definition {definition} contains a comma, and commas are not allowed, aborting"
                    )
                # validation the definition regardless of inclusion
                try:
                    log.trace(f"validating definition: {definition}")
                    body["name"] = definition
                    config = Definition.model_validate(body)
                except ValidationError as e:
                    handle_config_error(e)

                if len(limiter) > 0 and definition not in limiter:
                    if not config.always_include:
                        log.trace(f"definition {definition} not in limiter, skipping")
                        continue
                    log.trace(f"definition {definition} has always_include")
                log.trace(f"adding definition {definition} to definitions")
                self._definitions[definition] = config
            self._initialized = True

    def __len__(self):
        return len(self._definitions)

    def __getitem__(self, key: str) -> "Definition":
        return self._definitions[key]

    def __iter__(self):
        return iter(self._definitions)

    def __setitem__(self, key: str, value: "Definition"):
        if self._frozen:
            raise FrozenInstanceError("Cannot modify a frozen instance.")
        self._definitions[key] = value

    def freeze(self):
        self._frozen = True

    @classmethod
    def reset(cls):
        with cls._lock:
            cls._instance = None
            cls._frozen = False

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, handler(dict))
