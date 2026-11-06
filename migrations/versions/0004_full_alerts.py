"""RAM/disk "almost full" alerts replace the shared-machine high-usage alert

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

EMPTY = sa.text("'[]'::jsonb")


def upgrade() -> None:
    op.add_column("metric_snapshots", sa.Column("disks", JSONB, nullable=False,
                                                server_default=EMPTY))
    op.add_column("bookings", sa.Column("ram_alerted_at", sa.DateTime(timezone=True)))
    op.add_column("bookings", sa.Column("disk_alerted", JSONB, nullable=False,
                                        server_default=EMPTY))
    op.drop_column("bookings", "spike_alerted")


def downgrade() -> None:
    op.add_column("bookings", sa.Column("spike_alerted", sa.Boolean, nullable=False,
                                        server_default=sa.text("false")))
    op.drop_column("bookings", "disk_alerted")
    op.drop_column("bookings", "ram_alerted_at")
    op.drop_column("metric_snapshots", "disks")
