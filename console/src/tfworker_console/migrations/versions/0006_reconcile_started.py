"""when each store's last reconcile began

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

from tfworker_console.sqltypes import Timestamp

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stores", sa.Column("reconcile_started_at", Timestamp(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("stores", "reconcile_started_at")
