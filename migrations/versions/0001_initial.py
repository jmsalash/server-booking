"""initial schema

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
FALSE = sa.text("false")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("telegram_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("username", sa.String),
        sa.Column("display_name", sa.String, nullable=False),
        sa.Column("is_admin", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("dm_blocked", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("last_login_at", TS),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("expires_at", TS, nullable=False),
    )
    op.create_table(
        "login_tokens",
        sa.Column("token", sa.String, primary_key=True),
        sa.Column("telegram_id", sa.BigInteger),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("confirmed_at", TS),
        sa.Column("consumed_at", TS),
    )
    op.create_table(
        "servers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False, unique=True),
        sa.Column("hostname", sa.String, nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("grafana_instance", sa.String),
        sa.Column("has_gpu", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("gpu_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("shareable", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("notes", sa.Text),
        sa.Column("last_status_code", sa.String),
        sa.CheckConstraint("kind in ('grafana','standalone')", name="servers_kind_check"),
    )
    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("server_id", sa.Integer, sa.ForeignKey("servers.id"), nullable=False),
        sa.Column("start_at", TS, nullable=False),
        sa.Column("end_at", TS, nullable=False),
        sa.Column("resource", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False, server_default="active"),
        sa.Column("is_shared", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("idle_warnings", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("idle_since", TS),
        sa.Column("idle_ram_baseline", sa.Float),
        sa.Column("last_warning_at", TS),
        sa.Column("spike_alerted", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("start_reminder_sent", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("end_reminder_sent", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("calendar_event_id", sa.String),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("cancelled_at", TS),
        sa.Column("cancel_reason", sa.Text),
        sa.CheckConstraint("end_at > start_at", name="bookings_end_after_start"),
        sa.CheckConstraint("resource in ('cpu','cpu_gpu')", name="bookings_resource_check"),
        sa.CheckConstraint(
            "status in ('active','cancelled','reclaimed','completed')",
            name="bookings_status_check",
        ),
    )
    op.execute(
        "ALTER TABLE bookings ADD CONSTRAINT bookings_no_overlap "
        "EXCLUDE USING gist (server_id WITH =, tstzrange(start_at, end_at) WITH &&) "
        "WHERE (status = 'active' AND NOT is_shared)"
    )
    op.create_table(
        "metric_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("server_id", sa.Integer, sa.ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("cpu_pct", sa.Float),
        sa.Column("ram_pct", sa.Float),
        sa.Column("gpus", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.create_index("ix_metric_snapshots_server_id", "metric_snapshots", ["server_id"])
    op.create_index("ix_metric_snapshots_ts", "metric_snapshots", ["ts"])
    op.create_table(
        "watch_subscriptions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("server_id", sa.Integer, sa.ForeignKey("servers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("user_id", "server_id", name="watch_user_server_uq"),
    )
    op.create_table(
        "telegram_topics",
        sa.Column("thread_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False, server_default=NOW),
    )
    op.create_table(
        "outbox_items",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("payload", pg.JSONB, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("next_attempt_at", TS, nullable=False, server_default=NOW),
        sa.Column("last_error", sa.Text),
        sa.Column("done_at", TS),
        sa.Column("failed_at", TS),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("value", pg.JSONB, nullable=False),
        sa.Column("is_secret", sa.Boolean, nullable=False, server_default=FALSE),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("actor_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.String, nullable=False),
        sa.Column("target", sa.String),
        sa.Column("details", pg.JSONB),
        sa.Column("ts", TS, nullable=False, server_default=NOW),
    )


def downgrade() -> None:
    for t in ("audit_log", "settings", "outbox_items", "telegram_topics", "watch_subscriptions",
              "metric_snapshots", "bookings", "servers", "login_tokens", "sessions", "users"):
        op.drop_table(t)
