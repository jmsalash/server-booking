"""servers.archived_at; names unique among live servers only

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("archived_at", sa.DateTime(timezone=True)))
    # 0001 declared `name ... unique=True`; Postgres named that constraint servers_name_key.
    op.drop_constraint("servers_name_key", "servers", type_="unique")
    op.create_index("servers_name_live_uq", "servers", ["name"], unique=True,
                    postgresql_where=sa.text("archived_at IS NULL"))


def downgrade() -> None:
    # Fails if an archived server shares its name with another server; purge it first.
    op.drop_index("servers_name_live_uq", table_name="servers")
    op.create_unique_constraint("servers_name_key", "servers", ["name"])
    op.drop_column("servers", "archived_at")
