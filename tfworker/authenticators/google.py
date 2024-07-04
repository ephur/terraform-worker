import shlex

from .base import BaseAuthenticator, BaseAuthenticatorConfig


class GoogleAuthenticatorConfig(BaseAuthenticatorConfig):
    gcp_creds_path: str
    gcp_region: str
    project: str


class GoogleAuthenticator(BaseAuthenticator):
    tag = "google"
    config_model = GoogleAuthenticatorConfig

    def __init__(self, state_args, **kwargs):
        super(GoogleAuthenticator, self).__init__(state_args, **kwargs)

        self.creds_path = self._resolve_arg("gcp_creds_path")
        self.project = self._resolve_arg("gcp_project")
        self.region = self._resolve_arg("gcp_region")

    def env(self):
        result = {}
        if self.creds_path:
            result["GOOGLE_APPLICATION_CREDENTIALS"] = shlex.quote(self.creds_path)
        return result


class GoogleBetaAuthenticator(GoogleAuthenticator):
    tag = "google-beta"
