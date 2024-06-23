from typing import Callable, Dict, List

from pydantic import BaseModel

from tfworker.exceptions import HandlerError

from .base import BaseHandler


class HandlerRegistry:
    """
    All handlers must be registered in order to be available for selection in an execution
    """

    _registry = {}
    _universal = []

    @classmethod
    def register(cls, name: str, always: bool = False) -> Callable:
        """
        Class method to register handlers
        """

        def inner_wrapper(wrapped_class: BaseHandler) -> Callable:
            if name in cls._registry:
                raise ValueError(f"Handler {name} already exists")
            cls._registry[name] = wrapped_class
            if always:
                cls._universal.append(name)
            return wrapped_class

        return inner_wrapper

    @classmethod
    def list_universal_handlers(cls) -> List[str]:
        """
        get_universal_handlers returns all of the registered universal handlers
        """
        return cls._universal

    @classmethod
    def get_handler(cls, name: str) -> BaseHandler:
        """
        get_handler returns a handler type that supports the provided name
        """
        return cls._registry[name]

    @classmethod
    def get_handlers(cls) -> Dict[str, BaseHandler]:
        """
        get_handlers returns all of the registered handlers
        """
        return cls._registry

    @classmethod
    def get_handler_names(cls) -> List[str]:
        """
        get_handler_names returns all of the registered handler names
        """
        return list(cls._registry.keys())

    @classmethod
    def get_handler_config_model(cls, name: str) -> BaseModel:
        """
        get_handler_config_model returns the config model for the handler
        """
        try:
            return cls._registry[name].config_model
        except KeyError:
            raise HandlerError(f"Handler {name} not found")

    @classmethod
    def match_handler(cls, config_model: BaseModel) -> BaseHandler:
        """
        match_handler returns the handler that matches the config model
        """
        for handler in cls._registry.values():
            if handler.config_model == config_model:
                return handler
        raise ValueError("No handler found for config model")

    @classmethod
    def make_handler(cls, name: str, config: BaseModel) -> BaseHandler:
        """
        make_handler returns a new handler instance
        """
        return cls._registry[name](config)
