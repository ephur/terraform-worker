"""deployment groups

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deployments", sa.Column("group_name", sa.String(length=64), nullable=True)
    )
    op.create_index(
        op.f("ix_deployments_group_name"), "deployments", ["group_name"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_deployments_group_name"), table_name="deployments")
    op.drop_column("deployments", "group_name")
