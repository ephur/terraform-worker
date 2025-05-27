import pytest

from tfworker.definitions import Definition, DefinitionsCollection
from tfworker.exceptions import FrozenInstanceError

mock_definitions = {
    "def1": {"path": "path1"},
    "def2": {"path": "path2"},
}


class TestDefinitionsCollection:
    def teardown_method(self):
        DefinitionsCollection.reset()

    def test_init(self):
        def1 = Definition(name="def1", path="path1")
        def2 = Definition(name="def2", path="path2")
        definitions_collection = DefinitionsCollection(mock_definitions)
        assert definitions_collection._definitions == {"def1": def1, "def2": def2}

    def test_init_with_comma_in_definition_name(self):
        with pytest.raises(ValueError):
            DefinitionsCollection({"def,1": {"path": "path1"}})

    def test_init_is_singleton(self):
        definitions_collection1 = DefinitionsCollection(mock_definitions)
        definitions_collection2 = DefinitionsCollection(mock_definitions)
        assert definitions_collection1 is definitions_collection2

    def test_init_bad_definition(self):
        with pytest.raises(SystemExit):
            DefinitionsCollection({"def1": {"path": "path1", "bad_key": "bad_value"}})

    def test_init_with_limiter(self):
        definitions_collection = DefinitionsCollection(
            mock_definitions, limiter=["def1"]
        )
        assert definitions_collection._definitions == {
            "def1": Definition(name="def1", path="path1")
        }

    def test_init_with_limiter_always_include_definition(self):
        definitions_collection = DefinitionsCollection(
            {
                "def1": {"path": "path1", "always_include": True},
                "def2": {
                    "path": "path2",
                },
            },
            limiter=["def2"],
        )
        assert definitions_collection._definitions.keys() == {"def1", "def2"}

    def test_get(self):
        definitions_collection = DefinitionsCollection(mock_definitions)
        assert definitions_collection.get("def1") == Definition(
            name="def1", path="path1"
        )

    def test_get_not_found(self):
        definitions_collection = DefinitionsCollection(mock_definitions)
        assert definitions_collection.get("def3") is None

    def test_len(self):
        definitions_collection = DefinitionsCollection(mock_definitions)
        assert len(definitions_collection) == 2

    def test_iter(self):
        definitions_collection = DefinitionsCollection(mock_definitions)
        assert list(definitions_collection) == ["def1", "def2"]

    def test_setitem(self):
        definitions_collection = DefinitionsCollection(mock_definitions)
        definitions_collection["def3"] = Definition(name="def3", path="path3")
        assert definitions_collection.get("def3") == Definition(
            name="def3", path="path3"
        )

    def test_setitem_frozen(self):
        definitions_collection = DefinitionsCollection(mock_definitions)
        definitions_collection.freeze()
        with pytest.raises(FrozenInstanceError):
            definitions_collection["def3"] = Definition(name="def3", path="path3")
