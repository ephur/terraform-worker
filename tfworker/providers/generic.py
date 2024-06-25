from tfworker.providers.base import BaseProvider
from tfworker.types.provider import ProviderConfig


class GenericProvider(BaseProvider):
    tag = "worker-generic"
    requires_auth = False

    def __init__(self, body: ProviderConfig, tag: str) -> None:
        self.tag = tag
        super(GenericProvider, self).__init__(body)
