from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.types import StatusCode
from app.models import Booking, Server, User
from app.services import board, notify, watchlist
from app.settings_store import K, SettingsStore

START_REMINDER = timedelta(minutes=30)
END_REMINDER = timedelta(minutes=60)
# Completely free: nobody's booking is running. Only this fires watchers ("Notify me") and the
# topic's "now free" post. SHAREABLE (one person in, one more may join) is busy for both, so
# BOOKED <-> SHAREABLE (a partner joins or leaves) never fires or posts, and a watcher's
# subscription is kept until the machine is really free.
FREED = frozenset({StatusCode.AVAILABLE, StatusCode.BOOKED_SOON})
BUSY = frozenset({StatusCode.BOOKED, StatusCode.FREE_SOON, StatusCode.SHAREABLE})


async def _complete(db: AsyncSession, now: datetime) -> None:
    rows = await db.scalars(
        select(Booking).where(Booking.status == "active", Booking.end_at <= now)
    )
    for b in rows:
        b.status = "completed"
    await db.flush()


async def _reminders(db: AsyncSession, store: SettingsStore, now: datetime, tz: ZoneInfo) -> None:
    portal = await store.get(K.PORTAL_BASE_URL)
    rows = await db.execute(
        select(Booking, Server.name)
        .join(Server, Server.id == Booking.server_id)
        .where(Booking.status == "active", Booking.end_at > now, Server.archived_at.is_(None))
        .order_by(Booking.id)
    )
    for b, server_name in rows.all():
        if not b.start_reminder_sent and b.start_at - START_REMINDER <= now < b.start_at:
            b.start_reminder_sent = True
            owner = await db.get(User, b.user_id)
            hm = b.start_at.astimezone(tz).strftime("%H:%M")
            await notify.dm(db, owner, f"⏰ Your booking on {server_name} starts at {hm}.", now)
        if not b.end_reminder_sent and b.end_at - END_REMINDER <= now < b.end_at:
            b.end_reminder_sent = True
            owner = await db.get(User, b.user_id)
            hm = b.end_at.astimezone(tz).strftime("%H:%M")
            text = f"⏰ Your booking on {server_name} ends at {hm}."
            if portal:
                text += f" Extend: {portal.rstrip('/')}/me"
            await notify.dm(db, owner, text, now)
    await db.flush()


async def _transitions(db: AsyncSession, store: SettingsStore, now: datetime) -> None:
    for card in await board.build_board(db, store, now):
        server = await db.get(Server, card.server.id)
        new = card.status.code
        prev = StatusCode(server.last_status_code) if server.last_status_code else None
        server.last_status_code = new.value
        await db.flush()
        if new == StatusCode.UNAVAILABLE or prev == new:
            continue
        if new in FREED and prev not in FREED:
            await watchlist.fire(db, store, server, card.status, now)
        if prev in BUSY and new in FREED:
            await notify.post_topic(db, store, "freed", f"🟢 {server.name} is now free.", now)


async def _lock_rows(db: AsyncSession, now: datetime) -> None:
    """Take every row lock this job may need up front, in the global lock order.

    Lock order shared with the booking service (create/extend lock the server, then the
    booking) and the monitor jobs (bookings by id): server rows first, ordered by id, then
    booking rows, ordered by id. Without this, _complete/_reminders would lock bookings and
    _transitions would lock servers afterwards — the reverse of extend, i.e. a deadlock.
    The whole job is one transaction (run_job commits), so the locks are held until commit.
    """
    await db.execute(select(Server.id).order_by(Server.id).with_for_update())
    await db.execute(
        select(Booking.id)
        .where(
            Booking.status == "active",
            or_(Booking.end_at <= now + END_REMINDER, Booking.start_at <= now + START_REMINDER),
        )
        .order_by(Booking.id)
        .with_for_update()
    )


async def booking_lifecycle(db: AsyncSession, store: SettingsStore, now: datetime) -> None:
    rules = await store.get_rules()
    await _lock_rows(db, now)
    await _complete(db, now)
    await _reminders(db, store, now, ZoneInfo(rules.timezone))
    await _transitions(db, store, now)
