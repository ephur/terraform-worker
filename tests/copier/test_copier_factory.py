import os
import platform
import tempfile
from unittest.mock import patch

import pytest

from tfworker.copier.factory import Copier, CopyFactory

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
