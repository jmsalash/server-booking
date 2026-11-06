"""Deleting a server: archive it (history kept, hidden everywhere live), restore, or purge."""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Booking, MetricSnapshot, OutboxItem, Server, User, WatchSubscription
from app.services import booking as booking_svc
from app.services.calendar_sync import DELETE as CALENDAR_DELETE
from app.settings_store import SettingsStore


class CalendarCleanupPending(Exception):
    """Calendar events of this server's bookings are still being removed by the outbox."""


@dataclass(frozen=True)
class PurgeCounts:
    bookings: int
    snapshots: int
    subscriptions: int


def _upcoming(server_id: int, now: datetime):
    return (Booking.server_id == server_id, Booking.status == "active", Booking.end_at > now)


async def count_upcoming(db: AsyncSession, server_id: int, now: datetime) -> int:
    """Active bookings that have not ended yet (cancelled when the server is deleted)."""
    return await db.scalar(
        select(func.count()).select_from(Booking).where(*_upcoming(server_id, now))) or 0


async def _lock(db: AsyncSession, server_id: int) -> Server:
    server = await db.scalar(
        select(Server).where(Server.id == server_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if server is None:
        raise LookupError(f"server {server_id} not found")
    return server


async def archive(
    db: AsyncSession, store: SettingsStore, server_id: int, actor: User, now: datetime,
) -> list[int]:
    """Cancel the server's upcoming and running bookings (owners notified, Calendar events
    removed — the normal cancel path), drop its watchlist, hide it. Returns the cancelled ids.
    """
    server = await _lock(db, server_id)  # lock order: server first, then bookings
    if server.archived_at is not None:
        raise LookupError(f"server {server_id} not found")
    ids = list((await db.scalars(
        select(Booking.id).where(*_upcoming(server_id, now)).order_by(Booking.id)
    )).all())
    reason = f"The server {server.name} was removed."
    for booking_id in ids:
        await booking_svc.cancel_booking(db, store, booking_id, actor=actor, now=now,
                                         reason=reason, immediate=True)
    await db.execute(delete(WatchSubscription).where(WatchSubscription.server_id == server_id))
    server.archived_at = now
    server.enabled = False
    await db.flush()
    return ids


async def restore(db: AsyncSession, server_id: int) -> Server:
    """Un-archive; stays disabled until an admin enables it. Callers check name/machine clashes."""
    server = await _lock(db, server_id)
    if server.archived_at is None:
        raise LookupError(f"server {server_id} is not deleted")
    server.archived_at = None
    server.enabled = False
    await db.flush()
    return server


async def purge_counts(db: AsyncSession, server_id: int) -> PurgeCounts:
    async def n(model) -> int:
        return await db.scalar(
            select(func.count()).select_from(model).where(model.server_id == server_id)) or 0

    return PurgeCounts(await n(Booking), await n(MetricSnapshot), await n(WatchSubscription))


async def purge(db: AsyncSession, server_id: int) -> tuple[str, PurgeCounts]:
    """Erase an archived server and every row that refers to it. Returns (name, counts)."""
    server = await _lock(db, server_id)
    if server.archived_at is None:
        raise LookupError(f"server {server_id} is not deleted")
    booking_ids = select(Booking.id).where(Booking.server_id == server_id).scalar_subquery()
    pending = await db.scalar(
        select(func.count()).select_from(OutboxItem).where(
            OutboxItem.kind == CALENDAR_DELETE, OutboxItem.done_at.is_(None),
            OutboxItem.failed_at.is_(None),
            OutboxItem.payload["booking_id"].as_integer().in_(booking_ids),
        )
    )
    if pending:
        # The delete handler needs the booking row to find the event; wait for it.
        raise CalendarCleanupPending(server.name)
    counts = await purge_counts(db, server_id)
    for model in (WatchSubscription, MetricSnapshot, Booking):
        await db.execute(delete(model).where(model.server_id == server_id))
    name = server.name
    await db.delete(server)
    await db.flush()
    return name, counts
