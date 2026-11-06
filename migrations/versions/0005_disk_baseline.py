"""Disk warnings only for disks that fill up during a booking: NULL = no baseline yet

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("bookings", "disk_alerted", nullable=True, server_default=None)
    # Running bookings with nothing recorded take a fresh baseline at the next check, so a
    # disk that was already full does not suddenly warn.
    op.execute("UPDATE bookings SET disk_alerted = NULL "
               "WHERE status = 'active' AND disk_alerted = '[]'::jsonb")


def downgrade() -> None:
    op.execute("UPDATE bookings SET disk_alerted = '[]'::jsonb WHERE disk_alerted IS NULL")
    op.alter_column("bookings", "disk_alerted", nullable=False,
                    server_default=sa.text("'[]'::jsonb"))
