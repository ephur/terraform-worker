import os

import pytest

import tfworker.main


class TestMain:
    def test_state_add_arg(self):
        state = tfworker.main.State()
        state.add_arg("a", 1)
        assert state.args.a == 1

    def test_state_add_args(self):
        state = tfworker.main.State()
        state.add_args({"a": 1, "b": "two"})
        assert state.args.a == 1
        assert state.args.b == "two"

    def test_state_init(self):
        state = tfworker.main.State(args={"a": 1, "b": "two"})
        assert state.args.a == 1
        assert state.args.b == "two"

    def test_config_loader(self, state):
        expected_sections = ["providers", "terraform_vars", "definitions"]
        expected_tf_vars = {
            "vpc_cidr": "10.0.0.0/16",
            "region": "us-west-2",
            "domain": "test.domain.com",
        }
        # make a copy of the state so the loaded config is not attached to the fixture, flag it not to clean since
        # main object will already clean the tempdir the fixture creates
        test_config_file = os.path.join(
            os.path.dirname(__file__), "fixtures/test_config.yaml"
        )
        state.load_config(test_config_file)
        terraform_config = state.config.get("terraform")
        for section in expected_sections:
            assert section in terraform_config.keys()

        for k, v in expected_tf_vars.items():
            assert terraform_config["terraform_vars"][k] == v

    @pytest.mark.parametrize(
        "commands, exit_code, cwd, stdin, stdout, stderr",
        [
            ("/bin/true", 0, None, None, "", ""),
            ("/bin/false", 1, None, None, "", ""),
            ("/bin/echo foo", 0, None, None, "foo", ""),
            ("/bin/grep foo", 0, None, "foo", "foo", ""),
            ("/bin/pwd", 0, "/tmp", None, "/tmp", ""),
            (
                "/bin/cat /yisohwo0AhK8Ah ",
                1,
                None,
                None,
                "",
                "/bin/cat: /yisohwo0AhK8Ah: No such file or directory",
            ),
            (["/bin/echo foo", "/bin/grep foo"], 0, None, None, "foo", ""),
            (["/bin/echo foo", "/bin/grep bar"], 1, None, None, "", ""),
        ],
    )
    def test_run_pipe_exec(self, commands, exit_code, cwd, stdin, stdout, stderr):
        (return_exit_code, return_stdout, return_stderr) = tfworker.main.pipe_exec(
            commands, cwd=cwd, stdin=stdin
        )

        assert return_exit_code == exit_code
        assert return_stdout.rstrip() == stdout.encode()
        assert return_stderr.rstrip() == stderr.encode()

    @pytest.mark.parametrize(
        "var, expected",
        [
            ("//deployment//", "test-0001"),
            ("//aws-region//}}", "us-west-2"),
            ("//   aws-region //}}", "us-west-2"),
            ("//aws_region//}}", "us-west-2"),
            ("/aws_region/", "/aws_region/"),
            ("aws-region", "aws-region"),
        ],
    )
    def test_replace_vars(self, state, var, expected):
        assert tfworker.main.replace_vars(var, state.args) == expected
