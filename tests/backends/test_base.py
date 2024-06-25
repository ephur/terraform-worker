# import json
# from unittest.mock import patch

# import pytest

# import tfworker.backends.base as base


# @patch.multiple(base.BaseBackend, __abstractmethods__=set())
# def test_base_backend():
#     b = base.BaseBackend()
#     assert b.tag == "base"
#     assert b.clean("", "") is None
#     assert b.data_hcl([]) is None
#     assert b.hcl("") is None


# def test_validate_backend_empty(request):
#     with pytest.raises(base.BackendError):
#         base.validate_backend_empty({})

#     with open(f"{request.config.rootdir}/tests/fixtures/states/empty.tfstate") as f:
#         state = json.load(f)
#         assert base.validate_backend_empty(state) is True

#     with open(f"{request.config.rootdir}/tests/fixtures/states/occupied.tfstate") as f:
#         state = json.load(f)
#         assert base.validate_backend_empty(state) is False
