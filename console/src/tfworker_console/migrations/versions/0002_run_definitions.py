"""run definitions

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

from tfworker_console.sqltypes import Timestamp

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_definitions",
        sa.Column("store", sa.String(length=128), nullable=False),
        sa.Column("record_id", sa.String(length=255), nullable=False),
        sa.Column("definition", sa.String(length=255), nullable=False),
        sa.Column("deployment", sa.String(length=255), nullable=False),
        sa.Column("started_at", Timestamp(), nullable=False),
        sa.Column("phases", sa.JSON(), nullable=False),
        sa.Column("module_sources", sa.JSON(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("local_modules", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("store", "record_id", "definition"),
    )
    op.create_index(
        "ix_run_definitions_lookup",
        "run_definitions",
        ["store", "definition", "deployment", "started_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_run_definitions_started_at"),
        "run_definitions",
        ["started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_run_definitions_started_at"), table_name="run_definitions")
    op.drop_index("ix_run_definitions_lookup", table_name="run_definitions")
    op.drop_table("run_definitions")
