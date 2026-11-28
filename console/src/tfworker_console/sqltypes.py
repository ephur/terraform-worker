"""
Column types shared by the models and the migrations.

The console runs on any database SQLAlchemy supports; Postgres and SQLite are
tested. Types that need a per-engine form are declared here once, so a model
and its migration never disagree.
"""

from datetime import timezone

from sqlalchemy import DateTime
from sqlalchemy.dialects import mysql
from sqlalchemy.types import TypeDecorator

# engines whose timezone-aware DateTime stores the offset; the rest store a
# naive value, which the console always writes and reads as UTC
_STORES_OFFSET = frozenset({"postgresql", "oracle", "mssql"})


class Timestamp(TypeDecorator):
    """
    A timestamp stored and returned in UTC, with microseconds, on every engine.

    Values are moved to UTC before they are written, and an engine that keeps
    no offset gets the naive UTC value; whatever is read back is marked UTC.
    Ordering, filtering and the API's output never depend on the engine.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        # MySQL's DATETIME drops fractional seconds unless given a precision,
        # and the console orders and compares timestamps
        if dialect.name in ("mysql", "mariadb"):
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        return value if dialect.name in _STORES_OFFSET else value.replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
