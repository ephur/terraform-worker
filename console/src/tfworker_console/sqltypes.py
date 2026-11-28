"""
Column types shared by the models and the migrations.

The console runs on any database SQLAlchemy supports; Postgres and SQLite are
tested. Types that need a per-engine form are declared here once, so a model
and its migration never disagree.
"""

from sqlalchemy import DateTime
from sqlalchemy.dialects import mysql
from sqlalchemy.types import TypeDecorator


class Timestamp(TypeDecorator):
    """A timezone-aware timestamp that keeps microseconds on every engine."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        # MySQL's DATETIME drops fractional seconds unless given a precision,
        # and the console orders and compares timestamps
        if dialect.name in ("mysql", "mariadb"):
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=True))
