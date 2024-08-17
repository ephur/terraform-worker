import os
import re
import shutil

import tfworker.util.log as log

from .factory import Copier


class FileSystemCopier(Copier):
    _register_name = "fs"

    def copy(self, **kwargs) -> None:
        """copy copies files from a local source on the file system to a destination path"""
        dest = self.get_destination(**kwargs)
        self.check_conflicts(self.local_path)
        if "sub_path" in kwargs and kwargs["sub_path"]:
            source_path = f"{self.local_path}/{kwargs['sub_path']}".rstrip("/")
        else:
            source_path = self.local_path
        if not os.path.exists(source_path):
            raise FileNotFoundError(f"{source_path} does not exist")
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
        log.trace(f"type_matching fs copier for {source}")
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
