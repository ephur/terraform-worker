from abc import ABCMeta, abstractmethod


class BaseHandler(metaclass=ABCMeta):
    """The base handler class should be extended by all handler classes."""

    actions = []
    required_vars = []

    def is_ready(self):  # pragma: no cover
        """is_ready is called to determine if a handler is ready to be executed"""
        try:
            return self._ready
        except AttributeError:
            return False

    @abstractmethod
    def execute(self, action: str, stage: str, **kwargs) -> None:  # pragma: no cover
        """
        execute is called when a handler should trigger, it accepts to parameters
            action: the action that triggered the handler (one of plan, clean, apply, destroy)
            stage: the stage of the action (one of pre, post)
            kwargs: any additional arguments that may be required
        """
        pass

    def __str__(self):
        return self.__class__.__name__
