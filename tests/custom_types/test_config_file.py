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


class TestReporting:
    def test_off_by_default(self):
        assert ConfigFile().reporting is None

    def test_empty_block_turns_it_on_with_defaults(self):
        reporting = ConfigFile(reporting={}).reporting
        assert reporting.resources and reporting.state_serials
        assert reporting.tracked_ref is None
        assert reporting.status_retries == 5

    def test_rejects_unknown_key_and_bad_retries(self):
        with pytest.raises(ValidationError):
            ConfigFile(reporting={"bogus": 1})
        with pytest.raises(ValidationError):
            ConfigFile(reporting={"status_retries": 0})

    def test_frozen_with_the_config(self):
        config = ConfigFile(reporting={"resources": False}).freeze()
        with pytest.raises(Exception):
            config.reporting.resources = True
