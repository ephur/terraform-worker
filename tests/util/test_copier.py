# Copyright 2020-2023 Richard Maynard (richard.maynard@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import platform
import re
import shutil
import tempfile
from typing import Tuple
from unittest import mock
from unittest.mock import patch

import pytest

from tfworker.util.copier import Copier, CopyFactory, FileSystemCopier, GitCopier

C_CONFLICTS = ["test.txt", "foo", "test.tf"]
C_SOURCE = "test_source"
if platform.system() == "Darwin":
    C_ROOT_PATH = "/private/tmp/test"
else:
    C_ROOT_PATH = "/tmp/test/"


@pytest.fixture(scope="session")
def register_test_copier():
    @CopyFactory.register("testfixture")
    class TestCopierFixture(Copier):
        @staticmethod
        def type_match(source: str) -> bool:
            if source == "test":
                return True
            else:
                return False

        def copy(self) -> bool:
            return True


@pytest.fixture
@patch.multiple(Copier, __abstractmethods__=set())
def cwp(tmp_path):
    c = Copier(
        source=C_SOURCE,
        root_path=C_ROOT_PATH,
        destination=f"{str(tmp_path)}",
        conflicts=C_CONFLICTS,
        arbitrary="value",
    )
    return c


@pytest.fixture
@patch.multiple(Copier, __abstractmethods__=set())
def copier():
    c = Copier(source=C_SOURCE)
    return c


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


@pytest.mark.usefixtures("register_test_copier")
class TestCopierFactory:
    """tests for the copier factory"""

    def test_register(self):
        """test that copiers can register themselves"""
        start_len = len(CopyFactory.registry)

        @CopyFactory.register("test_copier")
        class TestCopier(Copier):
            pass

        assert len(CopyFactory.registry) == start_len + 1
        assert "test_copier" in CopyFactory.registry.keys()

        with pytest.raises(ValueError):

            @CopyFactory.register("test_copier")
            class TestCopier2(Copier):
                pass

    def test_get_copier_type(self):
        """test that get copier type functions for the test copier"""
        assert CopyFactory.get_copier_type("test") == "testfixture"

        with pytest.raises(NotImplementedError):
            CopyFactory.get_copier_type("invalid")

    def test_create_copier(self):
        """test that the proper object is returned given the test copier source"""
        assert type(CopyFactory.create("test")).__name__ == "TestCopierFixture"


class TestCopier:
    """tests for the base Copier class"""

    @patch.multiple(Copier, __abstractmethods__=set())
    def test_constructor(self, tmp_path, copier, cwp):
        """test that the copiers have expected properties"""
        assert copier._source == C_SOURCE
        assert not hasattr(copier, "_root_path")
        assert not hasattr(copier, "_destination")
        assert not hasattr(copier, "_conflicts")
        assert len(copier._kwargs) == 0

        assert cwp._source == C_SOURCE
        assert cwp._root_path == C_ROOT_PATH
        assert cwp._destination == str(tmp_path)
        assert cwp._conflicts == C_CONFLICTS
        assert cwp._kwargs["arbitrary"] == "value"

        with pytest.raises(ValueError):
            Copier(source="test_source", conflicts="bad_value")

    def test_source(self, copier, cwp):
        """test the source property"""
        assert copier.source == C_SOURCE
        assert cwp.source == C_SOURCE

    def test_root_path(self, copier, cwp):
        """test that root path always returns a string for all copiers"""
        assert cwp.root_path == C_ROOT_PATH
        assert copier.root_path == ""

    def test_conflicts(self, copier, cwp):
        """test to ensure conflicts property always returns a list, with contents depending on copier params"""
        assert copier.conflicts == []
        assert cwp.conflicts == C_CONFLICTS

    def test_check_conflicts(self, request, copier, cwp):
        """test the behavior of checking conflicts"""

        with pytest.raises(FileExistsError):
            cwp.check_conflicts(
                f"{request.config.rootdir}/tests/fixtures/definitions/test_a"
            )

        assert (
            cwp.check_conflicts(
                f"{request.config.rootdir}/tests/fixtures/definitions/test_c"
            )
            is None
        )
        assert (
            copier.check_conflicts(
                f"{request.config.rootdir}/tests/fixtures/definitions/test_c"
            )
            is None
        )
        assert (
            copier.check_conflicts(
                f"{request.config.rootdir}/tests/fixtures/definitions/test_a"
            )
            is None
        )

    def test_get_destination(self, tmp_path, copier):
        dpath = f"{str(tmp_path)}/destination_test)"

        # test that get destination raises an error if destination is not set
        with pytest.raises(ValueError):
            copier.get_destination()

        # test that get destination returns proper directory, and it is not created
        setattr(copier, "_destination", dpath)
        assert copier.get_destination(make_dir=False) == dpath
        assert not os.path.isdir(dpath)

        # test that the destination is created with this optional parameter
        assert copier.get_destination(make_dir=True) == dpath
        assert os.path.isdir(dpath)

    def test_get_destination_path(self, tmp_path, copier):
        """Ensure the destination path is returned properly when destination is set"""
        dpath_td = tempfile.TemporaryDirectory()
        dpath = dpath_td.name

        # ensure object is in valid state for test
        with pytest.raises(AttributeError):
            getattr(copier, "_destination")

        assert copier.get_destination(**{"destination": dpath}) == dpath

        # ensure the directory is returned properly when make_dirs is true, and no errors
        # are raised when the directory already exists
        rpath = copier.get_destination(**{"destination": dpath, "make_dir": True})
        assert rpath == dpath
        assert os.path.isdir(rpath)

        # remove the temporary directory
        del dpath_td


class TestGitCopier:
    """test the GitCopier copier"""

    def test_copy(self, request, tmp_path):
        with mock.patch(
            "tfworker.util.copier.pipe_exec", side_effect=mock_pipe_exec_clone
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
            "tfworker.util.copier.pipe_exec", side_effect=mock_pipe_exec_type_match
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


class TestFileSystemCopier:
    """Test the FileSystem copier"""

    def test_copy(self, request, tmp_path):
        """tests the file system copy method"""
        assert not os.path.isfile(f"{str(tmp_path)}/test.tf")
        c = FileSystemCopier(
            source="/tests/fixtures/definitions/test_a",
            root_path=f"{request.config.rootdir}",
            destination=f"{str(tmp_path)}",
        )
        c.copy()
        assert os.path.isfile(f"{str(tmp_path)}/test.tf")

        c = FileSystemCopier(
            source="/tests/fixtures/definitions/test_a",
            root_path=f"{request.config.rootdir}",
            destination=f"{str(tmp_path)}",
        )

        with pytest.raises(FileNotFoundError):
            c.copy(sub_path="invalid_path")

    def test_local_path(self):
        """tests the local path property"""

        # This is a relative path based on where the worker ran from
        source = "tests/fixtures/definitions/test_a"
        c = FileSystemCopier(source=source, root_path=os.getcwd())
        assert c.local_path == f"{os.getcwd()}/{source}"

        # This tests resolution of an absolute path
        tmpdir = tempfile.TemporaryDirectory()
        c = FileSystemCopier(source=tmpdir.name)
        assert c.local_path == tmpdir.name
        del tmpdir

        # Ensure file not found error is raised on invalid relative path
        with pytest.raises(FileNotFoundError):
            FileSystemCopier(
                source="some/invalid/path", root_path=os.getcwd()
            ).local_path

        # Ensure file not found error is raised on invalid absolute path
        with pytest.raises(FileNotFoundError):
            FileSystemCopier(source="/some/invalid/path").local_path

    def test_type_match(self, request):
        source = FileSystemCopier.make_local_path(
            source="/tests/fixtures/definitions/test_a",
            root_path=f"{request.config.rootdir}",
        )

        # this should return true because the source is a valid directory
        assert FileSystemCopier.type_match(source) is True
        # this should return false because the full path to source does not exist inside of root_path
        assert FileSystemCopier.type_match("/some/invalid/path") is False
        # this should return true because the full path to source exists inside of root_path
        assert (
            FileSystemCopier.type_match(
                "/tests/fixtures/definitions/test_a",
                **{"root_path": f"{request.config.rootdir}"},
            )
            is True
        )
        # this should return false because the source is not a valid directory
        assert FileSystemCopier.type_match("/some/invalid/path") is False

    @pytest.mark.parametrize(
        "source, root_path, expected",
        [
            ("bar", "/tmp/foo", "/tmp/foo/bar"),
            ("/tmp", "", "/tmp"),
            ("/bar//", "/tmp/", "/tmp/bar/"),
            ("//tmp//", "", "/tmp/"),
        ],
    )
    def test_make_local_path(self, source, root_path, expected):
        assert (
            FileSystemCopier.make_local_path(source=source, root_path=root_path)
            == expected
        )
