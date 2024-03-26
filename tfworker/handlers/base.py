from abc import ABCMeta, abstractmethod


class BaseHandler(metaclass=ABCMeta):
    """The base handler class should be extended by all handler classes."""

    actions = []
    required_vars = []

    @abstractmethod
    def is_ready(self):  # pragma: no cover
        """is_ready is called to determine if a handler is ready to be executed"""
        return True

    @abstractmethod
    def execute(self, action: str, stage: str, **kwargs) -> None:  # pragma: no cover
        """
        execute is called when a handler should trigger, it accepts to parameters
            action: the action that triggered the handler (one of plan, clean, apply, destroy)
            stage: the stage of the action (one of pre, post)
            kwargs: any additional arguments that may be required
        """
        pass


class UnknownHandler(Exception):
    """This is an excpetion that indicates configuration was attempted for a handler that is not supported."""

    def __init__(self, provider):
        self.provider = provider

    def __str__(self):
        return f"Unknown handler: {self.provider}"


class HandlerError(Exception):
    """This is an exception that indicates an error occurred while attempting to execute a handler."""

    def __init__(self, message):
        self.message = message

    def __str__(self):
        return f"Handler error: {self.message}"
