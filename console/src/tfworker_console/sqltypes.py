"""
Column types shared by the models and the migrations.

The console runs on any database SQLAlchemy supports; Postgres and SQLite are
tested. Types that need a per-engine form are declared here once, so a model
and its migration never disagree.
"""

from sqlalchemy import DateTime
from sqlalchemy.dialects import mysql

# MySQL's DATETIME drops fractional seconds unless given a precision, and the
# console orders and compares timestamps
Timestamp = DateTime(timezone=True).with_variant(
    mysql.DATETIME(fsp=6), "mysql", "mariadb"
)
