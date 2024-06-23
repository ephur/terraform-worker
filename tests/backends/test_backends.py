from tfworker.backends.backends import Backends


class TestBackendNames:
    def test_names(self):
        items = Backends.__members__.items()
        assert Backends.names() == [item[0] for item in items]
