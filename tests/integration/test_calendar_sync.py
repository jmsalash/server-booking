from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import OutboxItem
from app.services import calendar_sync as cs
from app.services.outbox import process_due
from app.settings_store import K

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


async def _configure(store):
    await store.set(K.GCAL_CALENDAR_ID, "cal@group.calendar.google.com")
    await store.set(K.PORTAL_BASE_URL, "https://book.lab")


async def test_title_format():
    assert cs.event_title("gpu-01", "Ana Rossi") == "gpu-01 | Ana Rossi"


async def test_create_stores_event_id(db, store, booking, fake_cal):
    await _configure(store)
    await cs.queue_create(db, store, booking.id, NOW)
    await process_due(db, cs.make_handlers(fake_cal), NOW)
    await db.refresh(booking)
    assert booking.calendar_event_id == "evt-1"
    ev = fake_cal.events["evt-1"]
    assert ev["title"] == "gpu-01 | Ana Rossi"
    assert ev["start"] == booking.start_at and ev["end"] == booking.end_at
    assert "Resource: CPU+GPU" in ev["description"]
    assert "Portal: https://book.lab" in ev["description"]
    assert "Shared booking" not in ev["description"]


async def test_shared_cpu_description(db, store, booking, fake_cal):
    await _configure(store)
    booking.resource, booking.is_shared = "cpu", True
    await cs.queue_create(db, store, booking.id, NOW)
    await process_due(db, cs.make_handlers(fake_cal), NOW)
    desc = fake_cal.events["evt-1"]["description"]
    assert "Resource: CPU\n" in desc + "\n" and "CPU+GPU" not in desc
    assert "Shared booking" in desc


async def test_update_and_delete(db, store, booking, fake_cal):
    await _configure(store)
    h = cs.make_handlers(fake_cal)
    await cs.queue_create(db, store, booking.id, NOW)
    await process_due(db, h, NOW)
    booking.end_at = booking.end_at + timedelta(hours=1)
    await cs.queue_update(db, store, booking.id, NOW)
    await process_due(db, h, NOW)
    assert fake_cal.events["evt-1"]["end"] == booking.end_at
    booking.status = "cancelled"
    await cs.queue_delete(db, store, booking.id, NOW)
    await process_due(db, h, NOW)
    assert fake_cal.events == {}
    await db.refresh(booking)
    assert booking.calendar_event_id is None


async def test_cancel_before_create_creates_nothing(db, store, booking, fake_cal):
    await _configure(store)
    await cs.queue_create(db, store, booking.id, NOW)
    booking.status = "cancelled"
    await cs.queue_delete(db, store, booking.id, NOW)
    await process_due(db, cs.make_handlers(fake_cal), NOW)
    assert fake_cal.events == {}


async def test_update_before_create_retries(db, store, booking, fake_cal):
    await _configure(store)
    await cs.queue_update(db, store, booking.id, NOW)
    await process_due(db, cs.make_handlers(fake_cal), NOW)
    item = (await db.execute(select(OutboxItem))).scalar_one()
    assert item.attempts == 1 and item.done_at is None
    await cs.queue_create(db, store, booking.id, NOW)
    booking.end_at = booking.end_at + timedelta(hours=2)
    h = cs.make_handlers(fake_cal)
    # update (lower id) runs before create, fails again; create then succeeds
    await process_due(db, h, NOW + timedelta(seconds=61))
    assert len(fake_cal.events) == 1
    await process_due(db, h, NOW + timedelta(seconds=300))
    assert fake_cal.events["evt-1"]["end"] == booking.end_at
    items = (await db.execute(select(OutboxItem))).scalars().all()
    assert len(items) == 2 and all(i.done_at is not None for i in items)


async def test_update_on_cancelled_booking_without_event_is_done(db, store, booking, fake_cal):
    await _configure(store)
    booking.status = "cancelled"
    await cs.queue_update(db, store, booking.id, NOW)
    await process_due(db, cs.make_handlers(fake_cal), NOW)
    item = (await db.execute(select(OutboxItem))).scalar_one()
    assert item.done_at is not None and item.attempts == 0
    assert fake_cal.events == {}


async def test_calendar_down_booking_kept_and_retried(db, store, booking, fake_cal):
    await _configure(store)
    h = cs.make_handlers(fake_cal)
    await cs.queue_create(db, store, booking.id, NOW)
    fake_cal.fail = True
    await process_due(db, h, NOW)
    item = (await db.execute(select(OutboxItem))).scalar_one()
    assert item.attempts == 1
    assert booking.status == "active"
    fake_cal.fail = False
    await process_due(db, h, NOW + timedelta(seconds=61))
    assert len(fake_cal.events) == 1


async def test_not_configured_enqueues_nothing(db, store, booking):
    await cs.queue_create(db, store, booking.id, NOW)
    await cs.queue_update(db, store, booking.id, NOW)
    await cs.queue_delete(db, store, booking.id, NOW)
    assert (await db.execute(select(OutboxItem))).first() is None
