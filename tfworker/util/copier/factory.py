from typing import Callable

from .copier import Copier


class CopyFactory:
    """The factory class for creating copiers"""

    registry = {}

    @classmethod
    def register(cls, name: str) -> Callable:
        """Class method to register copiers"""

        def inner_wrapper(wrapped_class: Copier) -> Callable:
            if name in cls.registry:
                raise ValueError(f"Executor {name} already exists")
            cls.registry[name] = wrapped_class
            return wrapped_class

        return inner_wrapper

    @classmethod
    def create(cls, source: str, **kwargs) -> "Copier":
        """create returns a copier type that supports handling the provided source"""
        copier_class = cls.registry[cls.get_copier_type(source, **kwargs)]
        copier = copier_class(source, **kwargs)
        return copier

    @classmethod
    def get_copier_type(cls, source: str, **kwargs) -> str:
        """get_copier_type tries to find a supported copier based on the provided source"""
        for copier_type, copier_class in cls.registry.items():
            if copier_class.type_match(source, **kwargs):
                return copier_type
        raise NotImplementedError(f"no valid copier for {source}")
