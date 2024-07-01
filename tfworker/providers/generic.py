from .base import BaseProvider
from .model import ProviderConfig


class GenericProvider(BaseProvider):
    tag = "worker-generic"
    requires_auth = False

    def __init__(self, body: ProviderConfig, tag: str) -> None:
        self.tag = tag
        super(GenericProvider, self).__init__(body)
