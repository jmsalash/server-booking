from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

TS = DateTime(timezone=True)
_now = func.now()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    dm_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(TS)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=_now)
    expires_at: Mapped[datetime] = mapped_column(TS)


class LoginToken(Base):
    __tablename__ = "login_tokens"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=_now)
    expires_at: Mapped[datetime] = mapped_column(TS)
    confirmed_at: Mapped[datetime | None] = mapped_column(TS)
    consumed_at: Mapped[datetime | None] = mapped_column(TS)


class Server(Base):
    __tablename__ = "servers"
    __table_args__ = (
        CheckConstraint("kind in ('grafana','standalone')", name="servers_kind_check"),
        # Names are unique among live servers only, so a deleted server's name can be reused.
        Index("servers_name_live_uq", "name", unique=True,
              postgresql_where=text("archived_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    hostname: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    grafana_instance: Mapped[str | None] = mapped_column(String)
    has_gpu: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    gpu_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    notes: Mapped[str | None] = mapped_column(Text)
    last_status_code: Mapped[str | None] = mapped_column(String)
    # Set when an admin deletes the server: hidden everywhere live, history kept.
    archived_at: Mapped[datetime | None] = mapped_column(TS)


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("end_at > start_at", name="bookings_end_after_start"),
        CheckConstraint("resource in ('cpu','cpu_gpu')", name="bookings_resource_check"),
        CheckConstraint(
            "status in ('active','cancelled','reclaimed','completed')",
            name="bookings_status_check",
        ),
        # A partner (who joined someone's booking) can't open it to a third person.
        CheckConstraint("NOT (shareable AND is_shared)", name="bookings_partner_not_shareable"),
        # Primaries never overlap; a partner overlaps only shareable bookings (checked by the
        # booking service under the server-row lock).
        ExcludeConstraint(
            ("server_id", "="),
            (func.tstzrange(text("start_at"), text("end_at")), "&&"),
            name="bookings_no_overlap",
            using="gist",
            where=text("status = 'active' AND NOT is_shared"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"))
    start_at: Mapped[datetime] = mapped_column(TS)
    end_at: Mapped[datetime] = mapped_column(TS)
    resource: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active", server_default="active")
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # The booker lets one other person book the same time (they become the partner).
    shareable: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    idle_warnings: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    idle_since: Mapped[datetime | None] = mapped_column(TS)
    idle_ram_baseline: Mapped[float | None] = mapped_column(Float)
    last_warning_at: Mapped[datetime | None] = mapped_column(TS)
    # RAM / disk "almost full" warnings already sent for this booking (re-armed when the value
    # drops back), so they are not repeated every poll.
    ram_alerted_at: Mapped[datetime | None] = mapped_column(TS)
    disk_alerted: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    start_reminder_sent: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    end_reminder_sent: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    calendar_event_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=_now)
    cancelled_at: Mapped[datetime | None] = mapped_column(TS)
    cancel_reason: Mapped[str | None] = mapped_column(Text)


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(TS, index=True)
    cpu_pct: Mapped[float | None] = mapped_column(Float)
    ram_pct: Mapped[float | None] = mapped_column(Float)
    gpus: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    disks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )


class WatchSubscription(Base):
    __tablename__ = "watch_subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "server_id", name="watch_user_server_uq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=_now)


class TelegramTopic(Base):
    __tablename__ = "telegram_topics"

    thread_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String)
    last_seen_at: Mapped[datetime] = mapped_column(TS, server_default=_now)


class OutboxItem(Base):
    __tablename__ = "outbox_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=_now)
    next_attempt_at: Mapped[datetime] = mapped_column(TS, server_default=_now)
    last_error: Mapped[str | None] = mapped_column(Text)
    done_at: Mapped[datetime | None] = mapped_column(TS)
    failed_at: Mapped[datetime | None] = mapped_column(TS)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String)
    target: Mapped[str | None] = mapped_column(String)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(TS, server_default=_now)
