# def test_google_hcl(basec, gcp_creds_file):
#     render = basec.providers["google"].hcl()
#     expected_render = f"""provider "google" {{
#   region = "us-west-2"
#   credentials = file("{gcp_creds_file}")
# }}"""

#     assert render == expected_render
