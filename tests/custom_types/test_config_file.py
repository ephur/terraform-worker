import pytest
from pydantic import ValidationError

from tfworker.custom_types.config_file import ConfigFile, DefinitionsSource


class TestDefinitionsSources:
    def test_defaults_to_empty(self):
        assert ConfigFile().definitions_sources == {}

    def test_accepts_sources(self):
        cfg = ConfigFile.model_validate(
            {
                "definitions_sources": {
                    "catalog": {
                        "path": "git@github.com:example/service-catalog.git",
                        "remote_path_options": {"branch": "main"},
                        "command": "bin/list-definitions",
                        "after": "network",
                    }
                }
            }
        )
        source = cfg.definitions_sources["catalog"]
        assert isinstance(source, DefinitionsSource)
        assert source.remote_path_options.branch == "main"
        assert source.after == "network"

    def test_optional_fields(self):
        source = DefinitionsSource(path="./catalog", command="run")
        assert source.remote_path_options is None
        assert source.after is None

    def test_rejects_unknown_key(self):
        with pytest.raises(ValidationError):
            ConfigFile.model_validate(
                {
                    "definitions_sources": {
                        "catalog": {"path": "./c", "command": "run", "bogus": 1}
                    }
                }
            )

    def test_requires_command(self):
        with pytest.raises(ValidationError):
            DefinitionsSource(path="./catalog")
