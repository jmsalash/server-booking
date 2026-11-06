"""sharing is chosen per booking: bookings.shareable; drop servers.shareable

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
    # Existing bookings become non-shareable: nobody chose to share them.
    op.add_column("bookings", sa.Column("shareable", sa.Boolean, nullable=False,
                                        server_default=sa.text("false")))
    op.create_check_constraint("bookings_partner_not_shareable", "bookings",
                               "NOT (shareable AND is_shared)")
    op.drop_column("servers", "shareable")


def downgrade() -> None:
    op.add_column("servers", sa.Column("shareable", sa.Boolean, nullable=False,
                                       server_default=sa.text("false")))
    op.drop_constraint("bookings_partner_not_shareable", "bookings", type_="check")
    op.drop_column("bookings", "shareable")
