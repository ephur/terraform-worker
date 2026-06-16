from unittest import mock

from tfworker.commands.terraform import TerraformResult
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.handlers.bitbucket import BitbucketHandler


def make_handler():
    handler = BitbucketHandler.__new__(BitbucketHandler)
    handler._required = False
    handler._pull_request = mock.Mock()
    handler._ready = True
    handler.config = mock.Mock()
    handler.config.pr_text = "Terraform Plan Output for {deployment} / {definition} \n---\n\n```\n{text}\n```"
    return handler


class TestBitbucketHandlerExecute:
    def test_execute_post_plan_exit_code_1_does_not_post(self):
        """With exit_code 1, execute must not post to the pull request."""
        handler = make_handler()
        definition = mock.Mock()
        definition.name = "mydef"
        definition.deployment = "dep"
        result = TerraformResult(1, b"error output", b"")

        handler.execute(
            action=TerraformAction.PLAN,
            stage=TerraformStage.POST,
            deployment="dep",
            definition=definition,
            working_dir="/tmp",
            result=result,
        )

        handler._pull_request.comment.assert_not_called()

    def test_execute_post_plan_exit_code_2_does_post(self):
        """With exit_code 2, execute must still post to the pull request."""
        handler = make_handler()
        definition = mock.Mock()
        definition.name = "mydef"
        definition.deployment = "dep"
        result = TerraformResult(
            2,
            b"Terraform will perform the following actions:\nPlan: 1 to add\n",
            b"",
        )

        handler.execute(
            action=TerraformAction.PLAN,
            stage=TerraformStage.POST,
            deployment="dep",
            definition=definition,
            working_dir="/tmp",
            result=result,
        )

        handler._pull_request.comment.assert_called_once()
