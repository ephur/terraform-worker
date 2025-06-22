from unittest import mock

from tfworker.commands.env import EnvCommand


class TestEnvCommand:
    def test_exec_outputs_env(self, capsys, mocker):
        app_state = mock.Mock()
        app_state.authenticators = [mock.Mock(env=lambda: {"A": "1"})]
        ctx = mock.Mock(obj=app_state)
        mocker.patch("click.get_current_context", return_value=ctx)
        mocker.patch("tfworker.commands.base.BaseCommand.__init__", return_value=None)

        cmd = EnvCommand(deployment="d")
        cmd._app_state = app_state
        cmd.exec()
        out = capsys.readouterr().out
        assert "export A=1" in out
