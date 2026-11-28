"""Apply the console's database migrations."""

from importlib import resources

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine


def alembic_config(engine: Engine) -> Config:
    config = Config()
    config.set_main_option(
        "script_location", str(resources.files("tfworker_console") / "migrations")
    )
    # configparser reads % as interpolation; URL-encoded passwords carry it
    config.set_main_option(
        "sqlalchemy.url", engine.url.render_as_string(False).replace("%", "%%")
    )
    return config


# any constant shared by every console; it names the lock below
MIGRATION_LOCK = 0x7466776B


def upgrade(engine: Engine, revision: str = "head") -> None:
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":  # pragma: no cover
            # replicas starting together take turns: the first migrates, and
            # the rest find the schema current. CI's Postgres job runs this.
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK}
            )
        config = alembic_config(engine)
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
