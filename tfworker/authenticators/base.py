from abc import ABC, abstractmethod, abstractproperty

from pydantic import BaseModel, Field


class BaseAuthenticatorConfig(BaseModel): ...


class BaseAuthenticator(ABC):
    tag: str
    config_model: BaseAuthenticatorConfig

    @abstractmethod
    def __init__(self, auth_config: BaseAuthenticatorConfig): ...

    @abstractmethod
    def env(self): ...


class UnknownAuthenticator(Exception):
    def __init__(self, provider):
        super().__init__(f"{provider} is not a known authenticator.")
