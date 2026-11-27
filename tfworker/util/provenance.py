"""Gather observable facts about where a run came from, for run records."""

import getpass
import json
import re
import socket
import subprocess
from importlib import metadata
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import tfworker.util.log as log
from tfworker.report import ModuleSource, Provenance, RepositoryState, new_ulid

GIT_TIMEOUT = 10
_REGISTRY = re.compile(r"^([\w.-]+/)?[\w-]+/[\w-]+/[\w-]+$")


def _git(cwd: Path, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug(f"git {' '.join(args)} failed in {cwd}: {e}")
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def repository_state(
    path: Path, tracked_ref: Optional[str] = None
) -> Optional[RepositoryState]:
    """
    Describe the git checkout at `path`: its commit, branch, whether it has
    local changes, and whether its commit is on the tracked ref (by default
    the remote's default branch, such as origin/main).

    Only local git data is used; nothing is fetched.
    """
    sha = _git(path, "rev-parse", "HEAD")
    if sha is None:
        return None
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git(path, "status", "--porcelain")
    if tracked_ref is None:
        tracked_ref = _git(path, "rev-parse", "--abbrev-ref", "origin/HEAD")
    on_tracked = None
    if tracked_ref:
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(path),
                    "merge-base",
                    "--is-ancestor",
                    "HEAD",
                    tracked_ref,
                ],
                capture_output=True,
                timeout=GIT_TIMEOUT,
                check=False,
            )
            if proc.returncode in (0, 1):
                on_tracked = proc.returncode == 0
        except (OSError, subprocess.SubprocessError) as e:
            log.debug(f"unable to compare HEAD with {tracked_ref}: {e}")
    return RepositoryState(
        sha=sha,
        branch=None if branch == "HEAD" else branch,
        dirty=None if status is None else bool(status),
        tracked_ref=tracked_ref or None,
        on_tracked_ref=on_tracked,
    )


def _is_local(source: str) -> bool:
    return source.startswith(("./", "../", "/")) or source in (".", "..")


def _git_ref(source: str) -> Optional[str]:
    query = urlsplit(source.split("::", 1)[-1]).query
    refs = parse_qs(query).get("ref")
    return refs[0] if refs else None


def _classify(source: str) -> str:
    if _is_local(source):
        return "local"
    if source.startswith(("git::", "git@", "github.com/", "bitbucket.org/")) or (
        ".git" in source.split("?", 1)[0]
    ):
        return "git"
    if _REGISTRY.match(source.split("//", 1)[0]):
        return "registry"
    return "other"


def _clone_root(root: Path, module_dir: Path) -> Optional[Path]:
    """
    The checkout a git module was fetched into. A `//subdir` source points
    below it, so walk up to the directory holding .git, never leaving
    .terraform/modules: above that, git would report an enclosing checkout.
    """
    modules = (root / ".terraform" / "modules").resolve()
    current = module_dir.resolve()
    while current != modules and modules in current.parents:
        if (current / ".git").exists():
            return current
        current = current.parent
    return None


def module_sources(target_dir: Path) -> List[ModuleSource]:
    """
    Read the modules terraform installed for a definition from
    `.terraform/modules/modules.json`.

    A module from a local path outside the definition (such as a mounted
    checkout of a module repository) is kind "local". A relative path inside
    the definition, or inside a module fetched from git or a registry, is part
    of that reviewed source and takes its parent's kind.
    """
    manifest = Path(target_dir) / ".terraform" / "modules" / "modules.json"
    try:
        entries = json.loads(manifest.read_text()).get("Modules") or []
    except (OSError, ValueError) as e:
        log.debug(f"no module manifest at {manifest}: {e}")
        return []

    root = Path(target_dir).resolve()
    by_key = {}
    out: List[ModuleSource] = []
    for entry in sorted(entries, key=lambda e: e.get("Key", "").count(".")):
        key = entry.get("Key", "")
        source = entry.get("Source", "")
        if not key:
            continue
        kind = _classify(source)
        ref = None
        sha = None
        parent = by_key.get(key.rsplit(".", 1)[0]) if "." in key else None
        if kind == "local":
            if parent is not None and parent.kind != "local":
                kind, ref, sha = parent.kind, parent.ref, parent.sha
            else:
                resolved = (root / entry.get("Dir", source)).resolve()
                if resolved == root or root in resolved.parents:
                    kind = "bundled"
        elif kind == "git":
            ref = _git_ref(source)
            clone = _clone_root(root, root / entry.get("Dir", ""))
            if clone is not None:
                sha = _git(clone, "rev-parse", "HEAD")
        elif kind == "registry":
            ref = entry.get("Version")
        module = ModuleSource(key=key, source=source, kind=kind, ref=ref, sha=sha)
        by_key[key] = module
        out.append(module)
    return out


_generated_run_id: Optional[str] = None


def current_run_id(app_state) -> Tuple[str, bool]:
    """
    The run's id and whether it was generated: `--run-id` when given,
    otherwise one ULID shared by everything this process records.
    """
    global _generated_run_id
    given = getattr(app_state.root_options, "run_id", None)
    if given:
        return given, False
    if _generated_run_id is None:
        _generated_run_id = new_ulid()
    return _generated_run_id, True


def plan_storage(app_state) -> str:
    if getattr(app_state.root_options, "backend_plans", False):
        return "backend"
    if getattr(app_state.terraform_options, "plan_file_path", None):
        return "local"
    return "run"


def run_provenance(app_state, tracked_ref: Optional[str] = None) -> Provenance:
    """Observable facts about the current run, for run records and plan manifests."""
    from tfworker.cli_options import parse_labels

    root = app_state.root_options
    tf = app_state.terraform_options
    config_files = getattr(root, "config_file", None) or []
    if isinstance(config_files, str):
        config_files = [config_files]
    repo = None
    if config_files:
        repo = repository_state(Path(config_files[0]).resolve().parent, tracked_ref)
    version = getattr(app_state, "terraform_version", None)
    if isinstance(version, tuple):
        version = ".".join(str(part) for part in version)
    return Provenance(
        labels=parse_labels(getattr(root, "run_label", None)),
        intent=getattr(tf, "intent", None),
        plan_storage=plan_storage(app_state),
        repository=repo,
        worker_version=_worker_version(),
        terraform_version=str(version) if version else None,
        host=socket.gethostname(),
        user=_user(),
    )


def _worker_version() -> Optional[str]:
    try:
        return metadata.version("terraform-worker")
    except metadata.PackageNotFoundError:
        return None


def _user() -> Optional[str]:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return None
