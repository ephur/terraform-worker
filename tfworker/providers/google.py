from .base import BaseProvider
from .model import ProviderConfig


class GoogleProvider(BaseProvider):
    tag = "google"
    requires_auth = True

    def __init__(self, body: ProviderConfig):
        super(GoogleProvider, self).__init__(body)

        self._authenticator = None

    def add_authenticators(self, authenticators: "AuthenticatorsCollection"):
        from tfworker.authenticators.collection import AuthenticatorsCollection

        self._authenticator = authenticators.get(self.tag)

        # if there is a creds file, tuck it into the provider vars
        if self._authenticator.creds_path:
            self.vars["credentials"] = f'file("{self._authenticator.creds_path}")'
