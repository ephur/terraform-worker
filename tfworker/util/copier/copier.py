from abc import ABCMeta, abstractmethod
from pathlib import Path

from tfworker.util.system import pipe_exec


class Copier(metaclass=ABCMeta):
    """The base class for definition copiers"""

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
        """type_match determins if the source is supported/handled by a copier"""
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
        """source contains the source path providede"""
        return self._source

    def get_destination(self, make_dir: bool = True, **kwargs) -> str:
        """get_destination returns the destination path, and optionally makes the destinatination directory"""
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
