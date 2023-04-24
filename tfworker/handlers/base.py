from abc import ABCMeta, abstractmethod


class BaseHandler(metaclass=ABCMeta):
    """The base handler class should be extended by all handler classes."""

    actions = []
    required_vars = []

    @abstractmethod
    def is_ready(self):  # pragma: no cover
        return True

    @abstractmethod
    def execute(self, action, **kwargs):  # pragma: no cover
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
