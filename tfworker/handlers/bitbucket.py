import os

from atlassian import Bitbucket
from atlassian.bitbucket import Cloud

from .base import BaseHandler
from .exceptions import HandlerError


class BitbucketHandler(BaseHandler):
    """
    The BitbucketHandler class is meant to interact with bitbucket cloud.

    Currently the only supported action is "plan" which will post a comment to a pull request with the output of a terraform plan.
    """

    # define supported actions
    actions = ["plan"]
    # define required variables / since different handlers may have different requirements we must rely on kwargs
    required_vars = [
        "username",
        "password",
        "workspace",
        "project",
        "repository",
        "pull_request",
    ]
    # define the text to be added to the pull request
    pr_text = "Terraform Plan Output for {deployment} / {definition} \n---\n\n```\n{text}\n```"

    def __init__(self, kwargs):
        # set ready to false until we are able to successfully get the pull request
        self._ready = False

        # ensure all of the required variables are set
        for var in self.required_vars:
            if var not in kwargs:
                raise HandlerError(
                    f"Missing required variable: {var}, required variabls are {','.join(sorted(self.required_vars))}"
                )

        # initialize the bitbucket object
        self._bb = Cloud(
            username=kwargs["username"], password=kwargs["password"], cloud=True
        )

        # get the workspace, project, and repository objects from bitbucket
        try:
            self._workspace = self._bb.workspaces.get(kwargs["workspace"])
            self._project = self._workspace.projects.get(kwargs["project"], by="name")
            self._repository = self._project.repositories.get(
                kwargs["repository"], by="name"
            )
        # TODO: catch specific exceptions
        # the exceptions raised by the bitbucket module are not well defined, so we will catch all exceptions for now
        except Exception as e:
            raise HandlerError(f"Error getting Bitbucket objects: {e}")

        # In the future more logic may be needed if we want to support sommething other than adding PR text as a comment
        # or if we want to support other mechanisms for supplying a pull request number other than from an environment variable
        p = kwargs["pull_request"].get("envvar", None)
        if p is not None:
            prnum = os.environ.get(p, None)
            if prnum is not None:
                self._pull_request = self._repository.pullrequests.get(prnum)
                self._ready = True

    def is_ready(self):
        """
        is_ready returns True if the handler is ready to execute actions, otherwise it returns False.
        """
        return self._ready

    def execute(self, action, **kwargs):
        """
        execute is a generic method that will execute the specified action with the provided arguments.
        """
        if action == "plan":
            for v in ["text", "planfile", "deployment", "definition"]:
                if v not in kwargs:
                    raise HandlerError(f"Missing required argument: {v}")

            self.plan(
                kwargs["deployment"],
                kwargs["definition"],
                kwargs["text"],
                kwargs["planfile"],
            )

    def plan(self, deployment, definition, text, planfile):
        """
        plan will post a comment to the pull request with the output of a terraform plan.
        """
        if self.is_ready():
            # mutate the text to only include planned changes
            capture = False
            trimmed_text = ""
            for line in text.splitlines():
                if line.startswith("Terraform will perform the following actions:"):
                    capture = True
                if line.startswith("Plan:"):
                    trimmed_text += line + "\n"
                    capture = False
                if capture:
                    trimmed_text += line + "\n"

            # add the comment to the pull request
            try:
                self._pull_request.comment(
                    self.pr_text.format(
                        deployment=deployment, definition=definition, text=trimmed_text
                    )
                )
            except Exception as e:
                raise HandlerError(f"Error adding comment to pull request: {e}")
        else:
            raise HandlerError("bitbucket handler not ready")
