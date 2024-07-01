import os
import re
import shutil
import tempfile

from tfworker.util.system import pipe_exec

from .factory import Copier


class GitCopier(Copier):
    _register_name = "git"

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
