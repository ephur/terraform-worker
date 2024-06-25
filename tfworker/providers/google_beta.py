from tfworker.providers.google import GoogleProvider


class GoogleBetaProvider(GoogleProvider):
    tag = "google-beta"
    requires_auth = True
