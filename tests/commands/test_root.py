from tfworker.cli_options import CLIOptionsRoot
from tfworker.commands.root import RootCommand


class TestRootHelpers:
    def test_resolve_working_dir_tmp(self, tmp_path, mocker):
        tmp = mocker.Mock()
        tmp.name = str(tmp_path)
        mocker.patch("tempfile.TemporaryDirectory", return_value=tmp)
        assert RootCommand._resolve_working_dir(None) == tmp_path

    def test_prepare_template_vars(self):
        opts = CLIOptionsRoot(
            config_file=[], aws_region="us-east-1", config_var=["foo=bar"]
        )
        res = RootCommand._prepare_template_vars(opts)
        assert res["aws_region"] == "us-east-1"
        assert res["foo"] == "bar"
