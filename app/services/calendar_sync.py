from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.gcal import CalendarClient
from app.models import Booking, Server, Setting, User
from app.services.outbox import Handler, enqueue
from app.settings_store import K, SettingsStore

CREATE, UPDATE, DELETE = "calendar_create", "calendar_update", "calendar_delete"


def event_title(server_name: str, user_display_name: str) -> str:
    return f"{server_name} | {user_display_name}"


async def _queue(db: AsyncSession, store: SettingsStore, kind: str, booking_id: int, now: datetime):
    if not await store.get(K.GCAL_CALENDAR_ID):
        return
    await enqueue(db, kind, {"booking_id": booking_id}, now)


async def queue_create(db, store, booking_id: int, now: datetime) -> None:
    await _queue(db, store, CREATE, booking_id, now)


async def queue_update(db, store, booking_id: int, now: datetime) -> None:
    await _queue(db, store, UPDATE, booking_id, now)


async def queue_delete(db, store, booking_id: int, now: datetime) -> None:
    await _queue(db, store, DELETE, booking_id, now)


async def _describe(db: AsyncSession, b: Booking) -> str:
    # PORTAL_BASE_URL is not a secret, so read the raw row (handlers get no SettingsStore).
    row = await db.get(Setting, K.PORTAL_BASE_URL)
    lines = ["Resource: CPU+GPU" if b.resource == "cpu_gpu" else "Resource: CPU"]
    if b.is_shared:
        lines.append("Shared booking")
    if row is not None and row.value:
        lines.append(f"Portal: {row.value}")
    return "\n".join(lines)


async def _fields(db: AsyncSession, b: Booking) -> tuple[str, str]:
    server = await db.get(Server, b.server_id)
    user = await db.get(User, b.user_id)
    return event_title(server.name, user.display_name), await _describe(db, b)


def make_handlers(client: CalendarClient) -> dict[str, Handler]:
    async def create(db: AsyncSession, payload: dict) -> None:
        b = await db.get(Booking, payload["booking_id"])
        if b is None or b.status != "active" or b.calendar_event_id:
            return
        title, desc = await _fields(db, b)
        b.calendar_event_id = await client.create_event(title, desc, b.start_at, b.end_at)

    async def update(db: AsyncSession, payload: dict) -> None:
        b = await db.get(Booking, payload["booking_id"])
        if b is None:
            return
        if not b.calendar_event_id:
            # create has not run yet: fail generically so the outbox retries later.
            raise RuntimeError(f"booking {b.id} has no calendar event yet")
        title, desc = await _fields(db, b)
        await client.update_event(b.calendar_event_id, title, desc, b.start_at, b.end_at)

    async def delete(db: AsyncSession, payload: dict) -> None:
        b = await db.get(Booking, payload["booking_id"])
        if b is None or not b.calendar_event_id:
            return
        await client.delete_event(b.calendar_event_id)
        b.calendar_event_id = None

    return {CREATE: create, UPDATE: update, DELETE: delete}
