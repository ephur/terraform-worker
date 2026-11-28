"""The schema stays generic: it renders for every engine the console may run on."""

import io
import re

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

from tfworker_console.db import Base
from tfworker_console.migrate import alembic_config

DIALECTS = {
    "postgresql": postgresql.dialect(),
    "mysql": mysql.dialect(),
    "sqlite": sqlite.dialect(),
}
URLS = {
    "postgresql": "postgresql://",
    "mysql": "mysql://",
    "sqlite": "sqlite://",
}


@pytest.mark.parametrize("name", sorted(DIALECTS))
def test_models_render_for_each_engine(name):
    for table in Base.metadata.sorted_tables:
        str(CreateTable(table).compile(dialect=DIALECTS[name]))


def _offline_sql(url: str) -> str:
    class FakeEngine:
        class url:
            @staticmethod
            def render_as_string(hide_password):
                return url

    config: Config = alembic_config(FakeEngine)
    buffer = io.StringIO()
    config.output_buffer = buffer
    command.upgrade(config, "head", sql=True)
    return buffer.getvalue()


@pytest.mark.parametrize("name", sorted(URLS))
def test_migrations_render_for_each_engine(name):
    assert "CREATE TABLE" in _offline_sql(URLS[name])


def test_mysql_timestamps_keep_microseconds():
    models = "".join(
        str(CreateTable(t).compile(dialect=DIALECTS["mysql"]))
        for t in Base.metadata.sorted_tables
    )
    for sql in (models, _offline_sql(URLS["mysql"])):
        assert "DATETIME(fsp=6)" not in sql
        assert "DATETIME(6)" in sql
        assert not re.search(r"DATETIME(?!\(6\))", sql)
