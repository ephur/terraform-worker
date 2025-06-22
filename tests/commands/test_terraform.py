from unittest import mock

from tfworker.commands.terraform import TerraformCommandConfig, TerraformResult
from tfworker.types.terraform import TerraformAction


class DummyAppState:
    class Opts:
        def __init__(self):
            self.stream_output = True
            self.terraform_bin = "/bin/terraform"
            self.b64_encode = False
            self.destroy = False
            self.apply = True
            self.strict_locking = True
            self.target = None
            self.color = False
            self.provider_cache = "/tmp"
    def __init__(self):
        self.terraform_options = self.Opts()
        self.root_options = mock.Mock(log_level="INFO")


class TestTerraformCommandConfig:
    def test_get_params_apply(self):
        cfg = TerraformCommandConfig(DummyAppState())
        params = cfg.get_params(TerraformAction.APPLY, "plan")
        assert "-auto-approve" in params

    def test_has_changes(self):
        r = TerraformResult(2, b"", b"")
        assert r.has_changes() is True
