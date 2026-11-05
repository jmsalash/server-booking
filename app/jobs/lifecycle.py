from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.types import StatusCode
from app.models import Booking, Server, User
from app.services import board, notify, watchlist
from app.settings_store import K, SettingsStore

START_REMINDER = timedelta(minutes=30)
END_REMINDER = timedelta(minutes=60)
FREE = frozenset({StatusCode.AVAILABLE, StatusCode.BOOKED_SOON, StatusCode.SHAREABLE})
FREED = frozenset({StatusCode.AVAILABLE, StatusCode.BOOKED_SOON})
BUSY = frozenset({StatusCode.BOOKED, StatusCode.FREE_SOON})


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
        .where(Booking.status == "active", Booking.end_at > now)
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
        if new in FREE and prev not in FREE:
            await watchlist.fire(db, store, server, card.status, now)
        if prev in BUSY and new in FREED:
            await notify.post_topic(db, store, "freed", f"🟢 {server.name} is now free.", now)


async def booking_lifecycle(db: AsyncSession, store: SettingsStore, now: datetime) -> None:
    rules = await store.get_rules()
    await _complete(db, now)
    await _reminders(db, store, now, ZoneInfo(rules.timezone))
    await _transitions(db, store, now)
