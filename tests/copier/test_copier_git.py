import os
import platform
import re
import shutil
from typing import Tuple
from unittest import mock

import pytest

from tfworker.copier import GitCopier

C_CONFLICTS = ["test.txt", "foo", "test.tf"]
C_SOURCE = "test_source"
if platform.system() == "Darwin":
    C_ROOT_PATH = "/private/tmp/test"
else:
    C_ROOT_PATH = "/tmp/test/"


def mock_pipe_exec_type_match(cmd: str) -> Tuple[int, str, str]:
    """a mock function to return specific results based on supplied command"""
    tokens = " ".join(cmd.split()).split(" ")
    if tokens[1] == "ls-remote":
        if tokens[2] == "permissionerror":
            raise PermissionError
        if tokens[2] == "filenotfounderror":
            raise FileNotFoundError
        if tokens[2] == "validremote":
            return (0, "", "")
    if tokens[0] == "/opt/bin/git":
        return (0, "", "")
    else:
        raise NotImplementedError("bad use of mock")


def mock_pipe_exec_clone(cmd: str, cwd: str) -> Tuple[int, str, str]:
    """a mock function to copy files and imitate a git clone"""
    tokens = re.split(r"\s+", cmd)
    assert os.path.isdir(tokens[2])
    shutil.copytree(tokens[2], cwd, dirs_exist_ok=True)
    return (0, "", "")


class TestGitCopier:
    """test the GitCopier copier"""

    def test_copy(self, request, tmp_path):
        with mock.patch(
            "tfworker.copier.git_copier.pipe_exec", side_effect=mock_pipe_exec_clone
        ) as mocked:
            """test a failing condition, conflicting files, no branch so check clone called with master"""
            dpath = f"{str(tmp_path)}/destination"
            spath = f"{request.config.rootdir}/tests/fixtures/definitions/test_a"
            c = GitCopier(source=spath, destination=dpath, conflicts=C_CONFLICTS)
            with pytest.raises(FileExistsError):
                c.copy()

            assert (
                mocked.call_args.args[0]
                == f"git clone {spath} --branch master --single-branch ./"
            )

            """ test a succeeding condition, extra options passed """
            spath = f"{request.config.rootdir}/tests/fixtures/definitions"
            c = GitCopier(source=spath, destination=dpath, conflicts=[])
            c.copy(
                branch="foo",
                sub_path="test_a",
                git_cmd="git",
                git_args="",
                reset_repo=True,
            )
            assert (
                mocked.call_args.args[0]
                == f"git clone {spath} --branch foo --single-branch ./"
            )
            assert os.path.isfile(f"{dpath}/test.tf")

    def test_type_match(self):
        """tests to ensure the various git cases return properly"""
        with mock.patch(
            "tfworker.copier.git_copier.pipe_exec",
            side_effect=mock_pipe_exec_type_match,
        ) as mocked:
            result = GitCopier.type_match("permissionerror")
            assert result is False
            mocked.assert_called_with("git  ls-remote permissionerror")

            result = GitCopier.type_match("filenotfounderror")
            assert result is False
            mocked.assert_called_with("git  ls-remote filenotfounderror")

            result = GitCopier.type_match(
                "string_inspect", git_cmd="/opt/bin/git", git_args="--bar"
            )
            assert result is True
            mocked.assert_called_with("/opt/bin/git --bar ls-remote string_inspect")

    def test_make_and_clean_temp(self):
        """tests making the temporary directory for git clones"""
        c = GitCopier("test_source")

        # ensure that the temp directory is created and attributes are set
        c.make_temp()
        assert hasattr(c, "_temp_dir")
        temp_dir = c._temp_dir
        assert os.path.isdir(temp_dir)
        assert hasattr(c, "_temp_dir")

        # ensure that the function is idempotent
        c.make_temp()
        # ensure that the temp directory is the same
        assert temp_dir == c._temp_dir
        assert os.path.isdir(c._temp_dir)
        assert hasattr(c, "_temp_dir")

        # ensure that the temp directory is removed
        c.clean_temp()
        assert not os.path.isdir(temp_dir)
        assert not hasattr(c, "_temp_dir")
