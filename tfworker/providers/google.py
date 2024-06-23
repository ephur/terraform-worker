from typing import TYPE_CHECKING

from .base import BaseProvider
from .model import ProviderConfig

if TYPE_CHECKING:
    from tfworker.authenticators import AuthenticatorsCollection


class GoogleProvider(BaseProvider):
    tag = "google"
    requires_auth = True

    def __init__(self, body: ProviderConfig):
        super(GoogleProvider, self).__init__(body)

        self._authenticator = None

    def add_authenticators(self, authenticators: "AuthenticatorsCollection"):
        self._authenticator = authenticators.get(self.tag)

        # if there is a creds file, tuck it into the provider vars
        if self._authenticator.creds_path:
            self.vars["credentials"] = f'file("{self._authenticator.creds_path}")'
