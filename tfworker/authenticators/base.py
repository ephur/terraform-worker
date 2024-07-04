from abc import ABC, abstractmethod

from pydantic import BaseModel


class BaseAuthenticatorConfig(BaseModel): ...


class BaseAuthenticator(ABC):
    tag: str
    config_model: BaseAuthenticatorConfig

    @abstractmethod
    def __init__(self, auth_config: BaseAuthenticatorConfig): ...

    @abstractmethod
    def env(self): ...
