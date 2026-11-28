import click
import uvicorn

from . import version
from .settings import ConsoleSettings


@click.group()
@click.version_option(version(), prog_name="tfworker-console")
def main() -> None:
    """Run the terraform-worker console."""


@main.command()
@click.option("--host", default=None, help="Interface to listen on")
@click.option("--port", default=None, type=int, help="Port to listen on")
@click.option("--reload", is_flag=True, help="Reload on code changes (development)")
def serve(host, port, reload) -> None:
    """Serve the console over HTTP."""
    settings = ConsoleSettings()
    uvicorn.run(
        "tfworker_console.app:create_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
    )
