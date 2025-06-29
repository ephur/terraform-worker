from abc import ABC, abstractmethod
from typing import Type

from pydantic import BaseModel


class BaseAuthenticatorConfig(BaseModel):
    """
    Base class for all authenticator configurations.
    """

    ...


class BaseAuthenticator(ABC):
    """
    Base class for all authenticators.
    """

    tag: str
    config_model: Type[BaseAuthenticatorConfig]

    @abstractmethod
    def __init__(self, auth_config: BaseAuthenticatorConfig): ...  # noqa

    @abstractmethod
    def env(self): ...  # noqa
