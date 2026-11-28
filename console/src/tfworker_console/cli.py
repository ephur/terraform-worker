import json
from dataclasses import asdict

import click
import uvicorn

from . import version
from .db import engine_for, session_factory
from .migrate import upgrade
from .reconcile import Reconciler
from .settings import ConsoleSettings
from .stores import load_stores


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


def _database_url(settings: ConsoleSettings) -> str:
    if not settings.database_url:
        raise click.UsageError("set TFWORKER_CONSOLE_DATABASE_URL")
    return settings.database_url


@main.group()
def db() -> None:
    """Manage the console's database."""


@db.command("upgrade")
def db_upgrade() -> None:
    """Create or migrate the database schema."""
    upgrade(engine_for(_database_url(ConsoleSettings())))
    click.echo("database is up to date")


@main.command()
@click.option("--store", "stores", multiple=True, help="Only this store (repeatable)")
@click.option(
    "--rebuild", is_flag=True, help="Empty the projection first, then reconcile all"
)
def reconcile(stores, rebuild) -> None:
    """Project the stores' run records, status, plans and locks into the database."""
    settings = ConsoleSettings()
    configured = load_stores(settings.stores_file)
    unknown = sorted(set(stores) - {store.name for store in configured})
    if unknown:
        raise click.UsageError(f"unknown store: {', '.join(unknown)}")
    reconciler = Reconciler(
        configured, session_factory(engine_for(_database_url(settings)))
    )
    if rebuild:
        if stores:
            raise click.UsageError("--rebuild reconciles every store")
        summaries = reconciler.rebuild()
    else:
        summaries = [reconciler.reconcile(name) for name in stores or reconciler.stores]
    click.echo(json.dumps([asdict(summary) for summary in summaries], indent=2))
    if any(summary.errors for summary in summaries):
        raise SystemExit(1)
