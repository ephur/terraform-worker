# # Copyright 2020 Richard Maynard (richard.maynard@gmail.com)
# #
# # Licensed under the Apache License, Version 2.0 (the "License");
# # you may not use this file except in compliance with the License.
# # You may obtain a copy of the License at
# #
# #     http://www.apache.org/licenses/LICENSE-2.0
# #
# # Unless required by applicable law or agreed to in writing, software
# # distributed under the License is distributed on an "AS IS" BASIS,
# # WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# # See the License for the specific language governing permissions and
# # limitations under the License.


# def test_google_hcl(basec, gcp_creds_file):
#     render = basec.providers["google"].hcl()
#     expected_render = f"""provider "google" {{
#   region = "us-west-2"
#   credentials = file("{gcp_creds_file}")
# }}"""

#     assert render == expected_render
