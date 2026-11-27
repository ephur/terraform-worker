import json
import os

import pytest
from pydantic import ValidationError

from tfworker.copier import CopyFactory
from tfworker.definitions import sources as s


def _stdout(mocker, *outputs):
    """Patch pipe_exec to return each output in turn."""
    return mocker.patch(
        "tfworker.definitions.sources.pipe_exec",
        side_effect=[(0, o.encode(), b"") for o in outputs],
    )


def _copier(mocker):
    """Patch the copier factory; returns the mocked create."""
    return mocker.patch.object(CopyFactory, "create")


def _destination(create):
    """The destination the mocked copier was asked to copy to."""
    return create.return_value.copy.call_args.kwargs["destination"]


class TestApplyDefinitionsSourcesLocal:
    def test_runs_command_in_temporary_copy(
        self, tmp_path, monkeypatch, definitions_source
    ):
        monkeypatch.setenv("SOURCE_TEST_VAR", "from-env")
        src = definitions_source
        config = {
            "definitions": {"base": {"path": "./base"}},
            "definitions_sources": {
                "local": {"path": str(src), "command": "bin/list-definitions"}
            },
        }

        s.apply_definitions_sources(config, "my-deployment", str(tmp_path))

        generated = config["definitions"]["generated"]
        copy_dir = generated["template_vars"].pop("cwd")
        assert generated["template_vars"] == {
            "deployment": "my-deployment",
            "inherited": "from-env",
        }
        assert copy_dir != str(src)
        assert not os.path.exists(copy_dir)
        assert generated["path"] == str(src)
        assert list(config["definitions"]) == ["base", "generated"]
        assert os.environ.get("WORKER_DEPLOYMENT") != "my-deployment"

    def test_relative_path_resolves_against_repository_path(
        self, tmp_path, definitions_source
    ):
        config = {
            "definitions": {},
            "definitions_sources": {
                "local": {"path": "catalog", "command": "bin/list-definitions"}
            },
        }

        s.apply_definitions_sources(config, "d", str(tmp_path))

        assert config["definitions"]["generated"]["path"] == "catalog"


class TestApplyDefinitionsSourcesMocked:
    def test_yaml_output(self, mocker):
        _copier(mocker)
        _stdout(mocker, "one:\n  path: ./one\ntwo:\n  path: ./two\n")
        config = {
            "definitions": {"base": {"path": "./base"}},
            "definitions_sources": {"src": {"path": "./x", "command": "run"}},
        }
        s.apply_definitions_sources(config, "d", ".")
        assert list(config["definitions"]) == ["base", "one", "two"]

    def test_json_output(self, mocker):
        _copier(mocker)
        _stdout(mocker, json.dumps({"one": {"path": "./one"}, "two": {}}))
        config = {
            "definitions": {},
            "definitions_sources": {"src": {"path": "./x", "command": "run"}},
        }
        s.apply_definitions_sources(config, "d", ".")
        assert config["definitions"] == {
            "one": {"path": "./one"},
            "two": {"path": "./x"},
        }

    def test_inserted_after_anchor_in_printed_order(self, mocker):
        _copier(mocker)
        _stdout(mocker, "zeta: {}\nalpha: {}\n")
        config = {
            "definitions": {
                "network": {"path": "./n"},
                "app": {"path": "./a"},
                "last": {"path": "./l"},
            },
            "definitions_sources": {
                "src": {"path": "./x", "command": "run", "after": "network"}
            },
        }
        s.apply_definitions_sources(config, "d", ".")
        assert list(config["definitions"]) == [
            "network",
            "zeta",
            "alpha",
            "app",
            "last",
        ]

    def test_appended_without_anchor(self, mocker):
        _copier(mocker)
        _stdout(mocker, "zeta: {}\nalpha: {}\n")
        config = {
            "definitions": {"network": {"path": "./n"}, "app": {"path": "./a"}},
            "definitions_sources": {"src": {"path": "./x", "command": "run"}},
        }
        s.apply_definitions_sources(config, "d", ".")
        assert list(config["definitions"]) == ["network", "app", "zeta", "alpha"]

    def test_missing_definitions_key(self, mocker):
        _copier(mocker)
        _stdout(mocker, "one: {}\n")
        config = {"definitions_sources": {"src": {"path": "./x", "command": "run"}}}
        s.apply_definitions_sources(config, "d", ".")
        assert config["definitions"] == {"one": {"path": "./x"}}

    def test_location_inheritance(self, mocker):
        _copier(mocker)
        _stdout(
            mocker,
            "inherits:\n  always_apply: true\n"
            "own:\n  path: git@github.com:example/other.git\n",
        )
        config = {
            "definitions": {},
            "definitions_sources": {
                "src": {
                    "path": "git@github.com:example/service-catalog.git",
                    "remote_path_options": {"branch": "main"},
                    "command": "run",
                }
            },
        }
        s.apply_definitions_sources(config, "d", ".")
        assert config["definitions"]["inherits"] == {
            "always_apply": True,
            "path": "git@github.com:example/service-catalog.git",
            "remote_path_options": {"branch": "main"},
        }
        assert config["definitions"]["own"] == {
            "path": "git@github.com:example/other.git"
        }

    def test_own_remote_path_options_kept(self, mocker):
        _copier(mocker)
        _stdout(mocker, "one:\n  remote_path_options:\n    sub_path: mod\n")
        config = {
            "definitions": {},
            "definitions_sources": {
                "src": {
                    "path": "./x",
                    "remote_path_options": {"branch": "main"},
                    "command": "run",
                }
            },
        }
        s.apply_definitions_sources(config, "d", ".")
        assert config["definitions"]["one"] == {
            "path": "./x",
            "remote_path_options": {"sub_path": "mod"},
        }

    def test_two_sources_in_config_order(self, mocker):
        _copier(mocker)
        pipe = _stdout(mocker, "first_gen: {}\n", "second_gen: {}\n")
        config = {
            "definitions": {"network": {"path": "./n"}, "app": {"path": "./a"}},
            "definitions_sources": {
                "first": {"path": "./one", "command": "run-one", "after": "network"},
                "second": {
                    "path": "./two",
                    "command": "run-two",
                    "after": "first_gen",
                },
            },
        }
        s.apply_definitions_sources(config, "d", ".")
        assert [c.args[0] for c in pipe.call_args_list] == ["run-one", "run-two"]
        assert list(config["definitions"]) == [
            "network",
            "first_gen",
            "second_gen",
            "app",
        ]
        assert config["definitions"]["second_gen"]["path"] == "./two"

    def test_command_environment(self, mocker, monkeypatch):
        monkeypatch.setenv("SOURCE_TEST_VAR", "inherited")
        create = _copier(mocker)
        pipe = _stdout(mocker, "one: {}\n")
        config = {
            "definitions": {},
            "definitions_sources": {"src": {"path": "./x", "command": "run --flag"}},
        }
        s.apply_definitions_sources(config, "the-deployment", ".")
        kwargs = pipe.call_args.kwargs
        assert kwargs["cwd"] == _destination(create)
        assert kwargs["env"]["WORKER_DEPLOYMENT"] == "the-deployment"
        assert kwargs["env"]["SOURCE_TEST_VAR"] == "inherited"
        assert pipe.call_args.args[0] == "run --flag"

    def test_branch_passed_to_copier(self, mocker):
        create = _copier(mocker)
        _stdout(mocker, "one: {}\n")
        config = {
            "definitions": {},
            "definitions_sources": {
                "src": {
                    "path": "git@github.com:example/service-catalog.git",
                    "remote_path_options": {"branch": "feature"},
                    "command": "run",
                }
            },
        }
        s.apply_definitions_sources(config, "d", "/repo")
        create.assert_called_once_with(
            "git@github.com:example/service-catalog.git",
            root_path="/repo",
            conflicts=[],
        )
        create.return_value.copy.assert_called_once_with(
            destination=mocker.ANY, branch="feature"
        )

    def test_no_remote_options_copies_without_options(self, mocker):
        create = _copier(mocker)
        _stdout(mocker, "one: {}\n")
        config = {
            "definitions": {},
            "definitions_sources": {"src": {"path": "./x", "command": "run"}},
        }
        s.apply_definitions_sources(config, "d", ".")
        create.return_value.copy.assert_called_once_with(destination=mocker.ANY)


def _config(**source):
    """A config with one configured definition and one source named src."""
    return {
        "definitions": {"network": {"path": "./n"}},
        "definitions_sources": {"src": {"path": "./x", "command": "run", **source}},
    }


class TestApplyDefinitionsSourcesErrors:
    def test_non_zero_exit(self, mocker):
        _copier(mocker)
        mocker.patch(
            "tfworker.definitions.sources.pipe_exec",
            return_value=(3, b"partial", b"something broke"),
        )
        with pytest.raises(s.DefinitionsSourceError) as e:
            s.apply_definitions_sources(_config(), "d", ".")
        assert "src" in str(e.value)
        assert "exited 3" in str(e.value)
        assert "something broke" in str(e.value)

    def test_stderr_logged_at_debug_on_success(self, mocker):
        _copier(mocker)
        mocker.patch(
            "tfworker.definitions.sources.pipe_exec",
            return_value=(0, b"one: {}\n", b"a warning"),
        )
        debug = mocker.patch("tfworker.util.log.debug")
        s.apply_definitions_sources(_config(), "d", ".")
        assert any("a warning" in str(c.args[0]) for c in debug.call_args_list)

    @pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
    def test_command_not_runnable(self, mocker, error):
        _copier(mocker)
        mocker.patch(
            "tfworker.definitions.sources.pipe_exec", side_effect=error("nope")
        )
        with pytest.raises(s.DefinitionsSourceError, match="src"):
            s.apply_definitions_sources(_config(), "d", ".")

    def test_missing_executable_for_real(self, definitions_source):
        config = {
            "definitions_sources": {
                "local": {
                    "path": str(definitions_source),
                    "command": "bin/does-not-exist",
                }
            }
        }
        with pytest.raises(s.DefinitionsSourceError, match="local"):
            s.apply_definitions_sources(config, "d", ".")

    def test_non_executable_for_real(self, definitions_source):
        (definitions_source / "bin" / "list-definitions").chmod(0o644)
        config = {
            "definitions_sources": {
                "local": {
                    "path": str(definitions_source),
                    "command": "bin/list-definitions",
                }
            }
        }
        with pytest.raises(s.DefinitionsSourceError, match="local"):
            s.apply_definitions_sources(config, "d", ".")

    @pytest.mark.parametrize(
        "output",
        [
            "one: [unclosed\n",
            "- one\n- two\n",
            "just a string\n",
            "one: not-a-mapping\n",
            "one: {}\ntwo: [a]\n",
            "1: {}\n",
        ],
    )
    def test_bad_output(self, mocker, output):
        _copier(mocker)
        _stdout(mocker, output)
        with pytest.raises(s.DefinitionsSourceError, match="src"):
            s.apply_definitions_sources(_config(), "d", ".")

    def test_empty_output_generates_nothing(self, mocker):
        _copier(mocker)
        _stdout(mocker, "")
        config = _config()
        s.apply_definitions_sources(config, "d", ".")
        assert list(config["definitions"]) == ["network"]

    def test_null_body_is_an_empty_definition(self, mocker):
        _copier(mocker)
        _stdout(mocker, "one:\n")
        config = _config()
        s.apply_definitions_sources(config, "d", ".")
        assert config["definitions"]["one"] == {"path": "./x"}

    def test_missing_anchor(self, mocker):
        create = _copier(mocker)
        pipe = _stdout(mocker, "one: {}\n")
        with pytest.raises(s.DefinitionsSourceError) as e:
            s.apply_definitions_sources(_config(after="nowhere"), "d", ".")
        assert "nowhere" in str(e.value)
        assert "src" in str(e.value)
        create.assert_not_called()
        pipe.assert_not_called()

    def test_collides_with_configured_definition(self, mocker):
        _copier(mocker)
        _stdout(mocker, "network: {}\n")
        with pytest.raises(s.DefinitionsSourceError) as e:
            s.apply_definitions_sources(_config(), "d", ".")
        assert "network" in str(e.value)
        assert "src" in str(e.value)
        assert "configured" in str(e.value)

    def test_same_name_from_two_sources(self, mocker):
        _copier(mocker)
        _stdout(mocker, "shared: {}\n", "shared: {}\n")
        config = {
            "definitions": {},
            "definitions_sources": {
                "first": {"path": "./one", "command": "run"},
                "second": {"path": "./two", "command": "run"},
            },
        }
        with pytest.raises(s.DefinitionsSourceError) as e:
            s.apply_definitions_sources(config, "d", ".")
        assert "shared" in str(e.value)
        assert "first" in str(e.value)
        assert "second" in str(e.value)

    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("unable to clone"),
            FileNotFoundError("missing"),
            FileExistsError("conflict"),
            PermissionError("denied"),
        ],
    )
    def test_copy_failure(self, mocker, error):
        create = _copier(mocker)
        create.return_value.copy.side_effect = error
        with pytest.raises(s.DefinitionsSourceError) as e:
            s.apply_definitions_sources(_config(), "d", ".")
        assert "src" in str(e.value)
        assert str(error) in str(e.value)

    def test_no_matching_copier(self, mocker):
        create = _copier(mocker)
        create.side_effect = NotImplementedError("no valid copier for ./x")
        with pytest.raises(s.DefinitionsSourceError, match="src"):
            s.apply_definitions_sources(_config(), "d", ".")

    def test_missing_local_path_for_real(self, tmp_path, monkeypatch):
        # other tests register fixture copiers on the shared registry
        registry = {k: v for k, v in CopyFactory.registry.items() if k in ("fs", "git")}
        monkeypatch.setattr(CopyFactory, "registry", registry)
        config = {
            "definitions_sources": {
                "local": {"path": str(tmp_path / "absent"), "command": "run"}
            }
        }
        with pytest.raises(s.DefinitionsSourceError, match="local"):
            s.apply_definitions_sources(config, "d", str(tmp_path))

    def test_file_path_for_real(self, tmp_path, monkeypatch):
        registry = {k: v for k, v in CopyFactory.registry.items() if k in ("fs", "git")}
        monkeypatch.setattr(CopyFactory, "registry", registry)
        (tmp_path / "catalog").write_text("not a directory")
        config = {
            "definitions_sources": {
                "local": {"path": str(tmp_path / "catalog"), "command": "run"}
            }
        }
        with pytest.raises(s.DefinitionsSourceError, match="local: unable to fetch"):
            s.apply_definitions_sources(config, "d", str(tmp_path))

    def test_unknown_key_in_source(self, mocker):
        create = _copier(mocker)
        with pytest.raises(ValidationError):
            s.apply_definitions_sources(_config(bogus=1), "d", ".")
        create.assert_not_called()


class TestTemporaryCopy:
    def _fetch_into(self, mocker):
        """Mock a copier that writes a file into its destination."""
        create = _copier(mocker)

        def copy(destination, **kwargs):
            with open(os.path.join(destination, "fetched"), "w") as f:
                f.write("x")

        create.return_value.copy.side_effect = copy
        return create

    def test_removed_after_success(self, mocker):
        create = self._fetch_into(mocker)
        seen = []

        def run(command, cwd, env):
            seen.append(os.path.exists(os.path.join(cwd, "fetched")))
            return 0, b"one: {}\n", b""

        mocker.patch("tfworker.definitions.sources.pipe_exec", side_effect=run)
        s.apply_definitions_sources(_config(), "d", ".")
        assert seen == [True]
        assert not os.path.exists(_destination(create))

    @pytest.mark.parametrize(
        "result",
        [(2, b"", b"broken"), (0, b"- not a mapping\n", b"")],
        ids=["non-zero-exit", "bad-output"],
    )
    def test_removed_after_command_failure(self, mocker, result):
        create = self._fetch_into(mocker)
        mocker.patch("tfworker.definitions.sources.pipe_exec", return_value=result)
        with pytest.raises(s.DefinitionsSourceError):
            s.apply_definitions_sources(_config(), "d", ".")
        assert not os.path.exists(_destination(create))

    @pytest.mark.parametrize(
        "error", [RuntimeError("unable to clone"), PermissionError("denied")]
    )
    def test_removed_after_fetch_failure(self, mocker, error):
        create = self._fetch_into(mocker)
        copy = create.return_value.copy.side_effect

        def failing_copy(destination, **kwargs):
            copy(destination)
            raise error

        create.return_value.copy.side_effect = failing_copy
        with pytest.raises(s.DefinitionsSourceError):
            s.apply_definitions_sources(_config(), "d", ".")
        assert not os.path.exists(_destination(create))

    def test_each_source_gets_its_own_copy(self, mocker):
        create = self._fetch_into(mocker)
        _stdout(mocker, "one: {}\n", "two: {}\n")
        config = {
            "definitions": {},
            "definitions_sources": {
                "first": {"path": "./one", "command": "run"},
                "second": {"path": "./two", "command": "run"},
            },
        }
        s.apply_definitions_sources(config, "d", ".")
        first, second = [
            c.kwargs["destination"] for c in create.return_value.copy.call_args_list
        ]
        assert first != second
