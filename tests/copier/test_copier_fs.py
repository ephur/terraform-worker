import os
import tempfile

import pytest

from tfworker.copier import FileSystemCopier


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

        # This tests the resolution of an absolute path with a root path
        tmpdir = tempfile.TemporaryDirectory()
        c = FileSystemCopier(source=tmpdir.name, root_path=os.getcwd())
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
