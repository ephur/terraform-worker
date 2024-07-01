from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Type


class CopyFactory:
    """The factory class for creating copiers"""

    registry = {}

    @classmethod
    def register(cls, name: str) -> Callable[[Type["Copier"]], Type["Copier"]]:
        """Class method to register copiers"""

        def inner_wrapper(wrapped_class: Type["Copier"]) -> Type["Copier"]:
            if name in cls.registry:
                raise ValueError(f"Executor {name} already exists")
            cls.registry[name] = wrapped_class
            return wrapped_class

        return inner_wrapper

    @classmethod
    def create(cls, source: str, **kwargs) -> "Copier":
        """
        create creates a copier based on the provided source

        Args:
            source (str): the source path to copy
            **kwargs: additional keyword arguments

        Returns:
            Copier: the copier instance
        """
        copier_class = cls.registry[cls.get_copier_type(source, **kwargs)]
        copier = copier_class(source, **kwargs)
        return copier

    @classmethod
    def get_copier_type(cls, source: str, **kwargs) -> str:
        """
        get_copier_type returns the copier type for a given source

        Args:
            source (str): the source path to copy
            **kwargs: additional keyword arguments

        Returns:
            str: the copier type
        """
        for copier_type, copier_class in cls.registry.items():
            if copier_class.type_match(source, **kwargs):
                return copier_type
        raise NotImplementedError(f"no valid copier for {source}")


class Copier(ABC):
    """The base class for definition copiers"""

    _register_name: str = None

    def __init__(self, source: str, **kwargs):
        self._source = source
        self._kwargs = {}

        for k, v in kwargs.items():
            if k in ["conflicts", "destination", "root_path"]:
                setattr(self, f"_{k}", v)
            else:
                self._kwargs[k] = v

        self._kwargs = kwargs

        if hasattr(self, "_conflicts"):
            if type(self._conflicts) is not list:
                raise ValueError("Conflicts must be a list of filenames to disallow")

    @staticmethod
    @abstractmethod
    def type_match(source: str, **kwargs) -> bool:  # pragma: no cover
        """type_match determines if the source is supported/handled by a copier"""
        pass

    @abstractmethod
    def copy(self, **kwargs) -> None:  # pragma: no cover
        """copy executes the copy from the source, into the working path"""
        pass

    @property
    def root_path(self):
        """root_path returns an optional root path to use for relative file operations"""
        if hasattr(self, "_root_path"):
            return self._root_path
        else:
            return ""

    @property
    def conflicts(self):
        """conflicts returns a list of disallowed files"""
        if hasattr(self, "_conflicts"):
            return self._conflicts
        else:
            return []

    @property
    def source(self):
        """source contains the source path provided"""
        return self._source

    def get_destination(self, make_dir: bool = True, **kwargs) -> str:
        """get_destination returns the destination path, and optionally makes the destination directory"""
        if not (hasattr(self, "_destination") or "destination" in kwargs.keys()):
            raise ValueError("no destination provided")
        if "destination" in kwargs:
            d = kwargs["destination"]
        else:
            d = self._destination

        if make_dir:
            make_d = Path(d)
            make_d.mkdir(parents=True, exist_ok=True)

        return d

    def check_conflicts(self, path: str) -> None:
        """Checks for files with conflicting names in a path"""
        conflicting = []
        if self.conflicts:
            check_path = Path(path)
            for check_file in check_path.glob("*"):
                if check_file.name in self.conflicts:
                    conflicting.append(check_file.name)

        if conflicting:
            raise FileExistsError(f"{','.join(conflicting)}")

    def __init_subclass__(cls, **kwargs):
        """
        Whenever a subclass is created, register it with the CopyFactory
        """
        super().__init_subclass__(**kwargs)
        copier_name = getattr(cls, "_register_name", None)
        if copier_name is not None:
            CopyFactory.register(copier_name)(cls)
