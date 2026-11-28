"""the audit log of admin actions

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

from tfworker_console.sqltypes import Timestamp

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("at", Timestamp(), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("store", sa.String(length=128), nullable=False),
        sa.Column("deployment", sa.String(length=255), nullable=False),
        sa.Column("definition", sa.String(length=255), nullable=False),
        sa.Column("group_name", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=2048), nullable=True),
        sa.Column("detail", sa.String(length=2048), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_log_at"), "audit_log", ["at"], unique=False)
    op.create_index(
        op.f("ix_audit_log_group_name"), "audit_log", ["group_name"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_log_group_name"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_at"), table_name="audit_log")
    op.drop_table("audit_log")
