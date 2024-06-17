def test_google_hcl(basec):
    render = basec.providers["google-beta"].hcl()
    expected_render = """provider "google-beta" {
  region = "us-west-2"
  credentials = file("/home/test/test-creds.json")
}"""

    assert render == expected_render
