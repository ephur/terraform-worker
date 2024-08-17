from tfworker.authenticators import (
    GoogleAuthenticator,
    GoogleAuthenticatorConfig,
    GoogleBetaAuthenticator,
)


class TestGoogleAuthenticator:
    def test_initialization(self):
        """Test that GoogleAuthenticator initializes correctly with config."""
        config = GoogleAuthenticatorConfig(
            gcp_creds_path="/path/to/creds.json",
            gcp_region="us-west-1",
            project="test-project",
        )
        authenticator = GoogleAuthenticator(auth_config=config)

        assert authenticator.creds_path == "/path/to/creds.json"
        assert authenticator.project == "test-project"
        assert authenticator.region == "us-west-1"

    def test_env_with_creds_path(self):
        """Test the env method returns correct environment variable."""
        config = GoogleAuthenticatorConfig(
            gcp_creds_path="/path/to/creds.json",
            gcp_region="us-west-1",
            project="test-project",
        )
        authenticator = GoogleAuthenticator(auth_config=config)

        env_vars = authenticator.env()
        assert env_vars["GOOGLE_APPLICATION_CREDENTIALS"] == "/path/to/creds.json"


class TestGoogleBetaAuthenticator:
    def test_tag_difference(self):
        """Test that GoogleBetaAuthenticator has a different tag than GoogleAuthenticator."""
        config = GoogleAuthenticatorConfig(
            gcp_creds_path="/path/to/creds.json",
            gcp_region="us-west-1",
            project="test-project",
        )
        beta_authenticator = GoogleBetaAuthenticator(auth_config=config)

        assert beta_authenticator.tag == "google-beta"
        assert GoogleAuthenticator.tag != GoogleBetaAuthenticator.tag
