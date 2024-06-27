import os
import re
import shutil
import tempfile
from abc import ABCMeta, abstractmethod, abstractstaticmethod
from pathlib import Path
from typing import Callable

from tfworker.util.system import pipe_exec


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

    @abstractstaticmethod
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


@CopyFactory.register("git")
class GitCopier(Copier):
    def copy(self, **kwargs) -> None:
        """copy clones a remote git repo, and puts the requested files into the destination"""
        dest = self.get_destination(**kwargs)
        branch = "master"
        git_cmd = "git"
        git_args = ""
        reset_repo = False

        sub_path = ""
        if "sub_path" in kwargs:
            sub_path = kwargs["sub_path"].strip("/")

        if "branch" in kwargs:
            branch = kwargs["branch"]
        if "git_cmd" in kwargs:
            git_cmd = kwargs["git_cmd"]
        if "git_args" in kwargs:
            git_args = kwargs["git_args"]
        if "reset_repo" in kwargs:
            reset_repo = kwargs["reset_repo"]

        self.make_temp()
        temp_path = f"{self._temp_dir}/{sub_path}"
        exitcode, stdout, stderr = pipe_exec(
            re.sub(
                r"\s+",
                " ",
                f"{git_cmd} {git_args} clone {self._source} --branch {branch} --single-branch ./",
            ),
            cwd=self._temp_dir,
        )

        if exitcode != 0:
            self.clean_temp()
            raise RuntimeError(
                f"unable to clone {self._source}, {stderr.decode('utf-8')}"
            )

        try:
            self.check_conflicts(temp_path)
        except FileExistsError as e:
            self.clean_temp()
            raise e

        if reset_repo:
            self.repo_clean(f"{temp_path}")

        shutil.copytree(temp_path, dest, dirs_exist_ok=True)
        self.clean_temp()

    @staticmethod
    def type_match(source: str, **kwargs) -> bool:
        # if the remote is a local file, then it's not a git repo
        if os.path.exists(source):
            return False

        """type matches uses git to see if the source is a valid git remote"""
        git_cmd = "git"
        git_args = ""

        if "git_cmd" in kwargs:
            git_cmd = kwargs["git_cmd"]
        if "git_args" in kwargs:
            git_args = kwargs["git_args"]

        try:
            (return_code, _, _) = pipe_exec(f"{git_cmd} {git_args} ls-remote {source}")

        except (PermissionError, FileNotFoundError):
            return False
        if return_code == 0:
            return True
        return False

    def make_temp(self) -> None:
        if hasattr(self, "_temp_dir"):
            pass
        else:
            self._temp_dir = tempfile.mkdtemp()

    def clean_temp(self) -> None:
        """clean_temp removes the temporary path used by this copier"""
        if hasattr(self, "_temp_dir"):
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            del self._temp_dir

    @staticmethod
    def repo_clean(p: str) -> None:
        """repo_clean removes git and github files from a clone before doing the copy"""
        for f in [".git", ".github"]:
            try:
                shutil.rmtree(f"{p}/{f}")
            except FileNotFoundError:
                pass


@CopyFactory.register("fs")
class FileSystemCopier(Copier):
    def copy(self, **kwargs) -> None:
        """copy copies files from a local source on the file system to a destination path"""
        dest = self.get_destination(**kwargs)
        self.check_conflicts(self.local_path)
        source_path = f"{self.local_path}/{kwargs.get('sub_path', '')}".rstrip("/")
        if not os.path.exists(source_path):
            raise FileNotFoundError(f"{kwargs.get('sub_path')} does not exist")
        shutil.copytree(source_path, dest, dirs_exist_ok=True)

    @property
    def local_path(self):
        """local_path returns a complete local file system path"""
        if not hasattr(self, "_local_path"):
            # try with the root path explicitly provided
            local_path = self.make_local_path(self.source, self.root_path)
            if os.path.exists(local_path):
                self._local_path = local_path
                return self._local_path

            # try without a root path (this is when an absolute path is provided)
            local_path = self.make_local_path(self.source, "")
            if os.path.exists(local_path):
                self._local_path = local_path
                return self._local_path

        if not hasattr(self, "_local_path"):
            raise FileNotFoundError(f"unable to find {self.source}")

        return self._local_path

    @staticmethod
    def type_match(source: str, **kwargs) -> bool:
        # check if the source was provided as an absolute path
        if os.path.isdir(source) or os.path.isfile(source):
            return True

        # check if the source is relative to the root path
        if "root_path" in kwargs:
            source = FileSystemCopier.make_local_path(source, kwargs["root_path"])

            if os.path.isdir(source) or os.path.isfile(source):
                return True

        return False

    @staticmethod
    def make_local_path(source: str, root_path: str) -> str:
        """make_local_path appends together known path objects to provide a local path"""
        full_path = f"{root_path}/{source}"
        full_path = re.sub(r"/+", "/", full_path)
        return full_path
