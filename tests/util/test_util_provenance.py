import json
import subprocess

import pytest

from tfworker.util import provenance
from tfworker.util.provenance import module_sources, repository_state


def git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "worker.yaml").write_text("definitions: {}\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "one")
    return path


class TestRepositoryState:
    def test_not_a_repo(self, tmp_path):
        assert repository_state(tmp_path) is None

    def test_on_tracked_ref(self, repo):
        state = repository_state(repo, "main")
        assert len(state.sha) == 40
        assert state.branch == "main"
        assert state.dirty is False
        assert state.tracked_ref == "main"
        assert state.on_tracked_ref is True

    def test_temp_branch_with_changes(self, repo):
        git(repo, "checkout", "-q", "-b", "temp")
        (repo / "worker.yaml").write_text("changed: true\n")
        git(repo, "commit", "-q", "-am", "two")
        (repo / "worker.yaml").write_text("dirty: true\n")
        state = repository_state(repo, "main")
        assert state.branch == "temp"
        assert state.dirty is True
        assert state.on_tracked_ref is False

    def test_no_remote_default(self, repo):
        state = repository_state(repo)
        assert state.tracked_ref is None
        assert state.on_tracked_ref is None

    def test_detached_head(self, repo):
        git(repo, "checkout", "-q", "--detach")
        assert repository_state(repo, "main").branch is None

    def test_unknown_tracked_ref(self, repo):
        assert repository_state(repo, "nope").on_tracked_ref is None

    def test_git_missing(self, tmp_path, mocker):
        mocker.patch("subprocess.run", side_effect=FileNotFoundError("git"))
        assert repository_state(tmp_path) is None

    def test_merge_base_error(self, repo, mocker):
        real = subprocess.run

        def run(args, **kwargs):
            if "merge-base" in args:
                raise subprocess.TimeoutExpired(args, 10)
            return real(args, **kwargs)

        mocker.patch("subprocess.run", side_effect=run)
        assert repository_state(repo, "main").on_tracked_ref is None


def write_manifest(target, modules):
    path = target / ".terraform" / "modules"
    path.mkdir(parents=True)
    (path / "modules.json").write_text(json.dumps({"Modules": modules}))


class TestModuleSources:
    def test_no_manifest(self, tmp_path):
        assert module_sources(tmp_path) == []

    def test_classifies_sources(self, tmp_path, repo):
        target = tmp_path / "def"
        clone = target / ".terraform" / "modules" / "app"
        write_manifest(
            target,
            [
                {"Key": "", "Source": "", "Dir": "."},
                {
                    "Key": "app",
                    "Source": "git::https://example.com/mods.git//app?ref=v1.2.0",
                    "Dir": ".terraform/modules/app",
                },
                {
                    "Key": "app.child",
                    "Source": "./child",
                    "Dir": ".terraform/modules/app/child",
                },
                {"Key": "inner", "Source": "./modules/inner", "Dir": "modules/inner"},
                {
                    "Key": "mounted",
                    "Source": "/terraform-modules/net",
                    "Dir": "/terraform-modules/net",
                },
                {
                    "Key": "mounted.sub",
                    "Source": "./sub",
                    "Dir": "/terraform-modules/net/sub",
                },
                {
                    "Key": "vpc",
                    "Source": "registry.terraform.io/terraform-aws-modules/vpc/aws",
                    "Version": "5.1.0",
                    "Dir": ".terraform/modules/vpc",
                },
                {
                    "Key": "zip",
                    "Source": "s3::https://b/x.zip",
                    "Dir": ".terraform/modules/zip",
                },
                {
                    "Key": "nogit",
                    "Source": "git@example.com:o/r.git",
                    "Dir": ".terraform/modules/nogit",
                },
            ],
        )
        clone.mkdir(parents=True)
        git(clone, "init", "-q")
        git(clone, "commit", "-q", "--allow-empty", "-m", "m")
        found = {m.key: m for m in module_sources(target)}
        assert found["app"].kind == "git"
        assert found["app"].ref == "v1.2.0"
        assert len(found["app"].sha) == 40
        assert found["app.child"].kind == "git"
        assert found["app.child"].sha == found["app"].sha
        assert found["inner"].kind == "bundled"
        assert found["mounted"].kind == "local"
        assert found["mounted.sub"].kind == "local"
        assert found["vpc"].kind == "registry"
        assert found["vpc"].ref == "5.1.0"
        assert found["zip"].kind == "other"
        assert found["nogit"].kind == "git"
        assert found["nogit"].sha is None
        assert "" not in found

    def test_unreadable_manifest(self, tmp_path):
        path = tmp_path / ".terraform" / "modules"
        path.mkdir(parents=True)
        (path / "modules.json").write_text("{")
        assert module_sources(tmp_path) == []

    @pytest.mark.parametrize(
        "source,kind",
        [
            ("github.com/o/r", "git"),
            ("hashicorp/consul/aws", "registry"),
            ("..", "local"),
            ("https://example.com/m.zip", "other"),
        ],
    )
    def test_classify(self, source, kind):
        assert provenance._classify(source) == kind
