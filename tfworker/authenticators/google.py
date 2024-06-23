import shlex

from .base import BaseAuthenticator, BaseAuthenticatorConfig


class GoogleAuthenticatorConfig(BaseAuthenticatorConfig):
    """
    Configuration for the Google Authenticator.

    Attributes:
        gcp_creds_path (str): The path to the Google Cloud Platform credentials file.
        gcp_region (str): The region to use for the Google Cloud Platform.
        project (str): The Google Cloud Platform project to USE.
    """

    gcp_creds_path: str
    gcp_region: str
    project: str


class GoogleAuthenticator(BaseAuthenticator):
    """
    Authenticator for Google Cloud Platform. Authentication is only supported using
    a service account key file.

    Attributes:
        creds_path (str): The path to the Google Cloud Platform credentials file.
        project (str): The Google Cloud Platform project to USE.
        region (str): The region to use for the Google Cloud Platform.
    """

    tag = "google"
    config_model = GoogleAuthenticatorConfig

    def __init__(self, auth_config: GoogleAuthenticatorConfig):
        self.creds_path = auth_config.gcp_creds_path
        self.project = auth_config.project
        self.region = auth_config.gcp_region

    def env(self):
        result = {}
        if self.creds_path:
            result["GOOGLE_APPLICATION_CREDENTIALS"] = shlex.quote(self.creds_path)
        return result


class GoogleBetaAuthenticator(GoogleAuthenticator):
    """
    The Google Beta Authenticator is the same as the Google Authenticator, but with a different tag.
    """

    tag = "google-beta"
