"""Apply the console's database migrations."""

from importlib import resources

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine


def alembic_config(engine: Engine) -> Config:
    config = Config()
    config.set_main_option(
        "script_location", str(resources.files("tfworker_console") / "migrations")
    )
    config.set_main_option("sqlalchemy.url", engine.url.render_as_string(False))
    return config


def upgrade(engine: Engine, revision: str = "head") -> None:
    with engine.begin() as connection:
        config = alembic_config(engine)
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
