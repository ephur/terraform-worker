def test_google_hcl(basec, gcp_creds_file):
    render = basec.providers["google-beta"].hcl()
    expected_render = f"""provider "google-beta" {{
  region = "us-west-2"
  credentials = file("{gcp_creds_file}")
}}"""

    assert render == expected_render
