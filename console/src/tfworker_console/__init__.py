"""A console for terraform-worker runs, plans and locks."""

from importlib import metadata


def version() -> str:
    try:
        return metadata.version("tfworker-console")
    except metadata.PackageNotFoundError:  # pragma: no cover
        return "0.0.0"
