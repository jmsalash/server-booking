import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.domain.booking_rules import BookingError
from app.domain.types import ServerUsage
from app.models import AuditLog, Booking, OutboxItem, Server, User
from app.services import usage
from app.services.booking import (
    BookingRejected,
    ShareConfirmationRequired,
    acknowledge_still_using,
    cancel_booking,
    create_booking,
    end_early,
    extend,
    reclaim,
    user_bookings,
)
from app.settings_store import K, SettingsStore

UTC = timezone.utc
NOW = datetime(2026, 11, 5, 12, 20, tzinfo=UTC)  # Thu 05, 13:20 Europe/Rome
H = timedelta(hours=1)
GROUP, TOPIC = -100123, 7


def at(hour, day=5):
    return datetime(2026, 11, day, hour, 0, tzinfo=UTC)


async def _configure(store):
    await store.set(K.GCAL_CALENDAR_ID, "cal@group.calendar.google.com")
    await store.set(K.TG_GROUP_ID, GROUP)
    await store.set(K.TG_TOPIC_ID, TOPIC)


async def _items(db, kind=None):
    q = select(OutboxItem).order_by(OutboxItem.id)
    if kind:
        q = q.where(OutboxItem.kind == kind)
    return list((await db.scalars(q)).all())


async def _texts(db):
    return [(i.payload["chat_id"], i.payload["text"]) for i in await _items(db, "telegram_message")]


async def _audit(db):
    return list((await db.scalars(select(AuditLog).order_by(AuditLog.id))).all())


async def _add(db, user, server, start, end, **kw):
    b = Booking(user_id=user.id, server_id=server.id, start_at=start, end_at=end,
                resource=kw.pop("resource", "cpu"), **kw)
    db.add(b)
    await db.flush()
    return b


async def _fresh_low_usage(db, server, now=NOW, pct=20.0):
    for m in range(0, 60, 5):
        await usage.save_snapshot(db, server.id, ServerUsage(now - timedelta(minutes=m), pct, pct, ()))


async def test_create_ok_queues_calendar_and_topic(db, store, make_user, make_server):
    await _configure(store)
    user = await make_user(display_name="Ana Rossi")
    server = await make_server(name="gpu-01")
    b = await create_booking(db, store, user, server.id, at(14), at(18), "cpu", NOW)
    assert (b.user_id, b.server_id, b.start_at, b.end_at, b.status, b.is_shared) == (
        user.id, server.id, at(14), at(18), "active", False,
    )
    [cal] = await _items(db, "calendar_create")
    assert cal.payload == {"booking_id": b.id}
    assert await _texts(db) == [(GROUP, "🔴 gpu-01 booked by Ana Rossi, Thu 05 15:00–Thu 05 19:00")]
    [a] = await _audit(db)
    assert (a.action, a.actor_user_id, a.target) == ("booking.create", user.id, f"booking:{b.id}")


async def test_create_validation_error_rejected(db, store, make_user, make_server):
    user = await make_user()
    server = await make_server()
    with pytest.raises(BookingRejected) as e:
        await create_booking(db, store, user, server.id, at(10), at(14), "cpu", NOW)
    assert e.value.error == BookingError.IN_PAST
    assert await _items(db) == []


async def test_inactive_user_cannot_create(db, store, make_user, make_server):
    user = await make_user(active=False)
    server = await make_server()
    with pytest.raises(PermissionError):
        await create_booking(db, store, user, server.id, at(14), at(18), "cpu", NOW)


async def test_conflict_rejected(db, store, make_user, make_server):
    ana, bob = await make_user(), await make_user()
    server = await make_server()
    await _add(db, ana, server, at(14), at(18))
    with pytest.raises(BookingRejected) as e:
        await create_booking(db, store, bob, server.id, at(16), at(20), "cpu", NOW)
    assert e.value.error == BookingError.CONFLICT


async def test_share_needs_confirmation_then_succeeds(db, store, make_user, make_server):
    await _configure(store)
    ana, bob = await make_user(display_name="ana"), await make_user(display_name="bob")
    server = await make_server(name="srv", kind="grafana", shareable=True)
    await _add(db, ana, server, at(11), at(16))
    await _fresh_low_usage(db, server)
    with pytest.raises(ShareConfirmationRequired) as e:
        await create_booking(db, store, bob, server.id, at(12), at(14), "cpu", NOW,
                             confirm_shared=False)
    assert e.value.share_with == "ana"
    assert await _items(db) == []
    b = await create_booking(db, store, bob, server.id, at(12), at(14), "cpu", NOW,
                             confirm_shared=True)
    assert b.is_shared
    assert await _texts(db) == [(GROUP, "🔴 srv booked by bob, Thu 05 13:00–Thu 05 15:00 (shared)")]


async def test_share_refused_when_metrics_stale(db, store, make_user, make_server):
    ana, bob = await make_user(display_name="ana"), await make_user()
    server = await make_server(kind="grafana", shareable=True)
    await _add(db, ana, server, at(11), at(16))
    await _fresh_low_usage(db, server, now=NOW - timedelta(minutes=30))
    with pytest.raises(BookingRejected) as e:
        await create_booking(db, store, bob, server.id, at(12), at(14), "cpu", NOW,
                             confirm_shared=True)
    assert e.value.error == BookingError.CONFLICT


async def test_concurrent_creates_only_one_wins(sessionmaker, box):
    async with sessionmaker() as s:
        u1 = User(telegram_id=501, display_name="u1")
        u2 = User(telegram_id=502, display_name="u2")
        srv = Server(name="race", hostname="race.lab", kind="standalone")
        s.add_all([u1, u2, srv])
        await s.commit()

    async def attempt(user):
        async with sessionmaker() as s:
            try:
                await create_booking(s, SettingsStore(s, box), user, srv.id, at(14), at(18),
                                     "cpu", NOW)
                await s.commit()
                return "ok"
            except BookingRejected as e:
                await s.rollback()
                return e.error

    results = await asyncio.gather(attempt(u1), attempt(u2))
    assert sorted(map(str, results)) == sorted(["ok", str(BookingError.CONFLICT)])
    async with sessionmaker() as s:
        rows = (await s.scalars(select(Booking))).all()
        assert len(rows) == 1


async def test_exclusion_constraint_maps_to_conflict(db, store, make_user, make_server,
                                                     monkeypatch):
    # Simulate a racing insert the validation did not see: hide existing bookings.
    from app.services import board

    ana, bob = await make_user(), await make_user()
    server = await make_server()
    await _add(db, ana, server, at(14), at(18))

    async def none(*a, **kw):
        return []

    monkeypatch.setattr(board, "active_bookings", none)
    with pytest.raises(BookingRejected) as e:
        await create_booking(db, store, bob, server.id, at(15), at(16), "cpu", NOW)
    assert e.value.error == BookingError.CONFLICT
    # Session still usable after the savepoint rollback.
    assert len((await db.scalars(select(Booking))).all()) == 1


async def test_end_early_rounds_up_to_next_hour(db, store, make_user, make_server):
    await _configure(store)
    user = await make_user()
    server = await make_server()
    b = await _add(db, user, server, at(11), at(17))
    await end_early(db, store, b.id, user, NOW)  # 12:20 UTC
    assert b.end_at == at(13) and b.status == "active"
    [upd] = await _items(db, "calendar_update")
    assert upd.payload == {"booking_id": b.id}
    [a] = await _audit(db)
    assert (a.action, a.actor_user_id) == ("booking.end_early", user.id)


async def test_end_early_at_start_hour_cancels(db, store, make_user, make_server):
    await _configure(store)
    user = await make_user()
    server = await make_server()
    b = await _add(db, user, server, at(12), at(17))
    await end_early(db, store, b.id, user, at(12))
    assert b.status == "cancelled" and b.end_at == at(17)
    assert len(await _items(db, "calendar_delete")) == 1
    assert await _items(db, "calendar_update") == []


async def test_end_early_on_ended_booking_is_noop(db, store, make_user, make_server):
    await _configure(store)
    user = await make_user()
    server = await make_server()
    b = await _add(db, user, server, at(9), at(11))
    await end_early(db, store, b.id, user, NOW)
    assert b.end_at == at(11) and b.status == "active"
    assert await _items(db) == [] and await _audit(db) == []


async def test_cancel_by_other_user_forbidden(db, store, make_user, make_server):
    ana, bob = await make_user(), await make_user()
    server = await make_server()
    b = await _add(db, ana, server, at(14), at(18))
    with pytest.raises(PermissionError):
        await cancel_booking(db, store, b.id, bob, NOW)
    assert b.status == "active"


async def test_owner_cancel_future_no_dm(db, store, booking):
    await _configure(store)
    owner = await db.get(User, booking.user_id)
    await cancel_booking(db, store, booking.id, owner, NOW)
    assert booking.status == "cancelled" and booking.cancelled_at == NOW
    assert len(await _items(db, "calendar_delete")) == 1
    assert await _texts(db) == []
    [a] = await _audit(db)
    assert (a.action, a.actor_user_id) == ("booking.cancel", owner.id)


async def test_admin_cancel_dms_owner(db, store, booking, make_user):
    await _configure(store)
    admin = await make_user(is_admin=True)
    owner = await db.get(User, booking.user_id)
    await cancel_booking(db, store, booking.id, admin, NOW, reason="maintenance")
    assert booking.status == "cancelled" and booking.cancel_reason == "maintenance"
    assert await _texts(db) == [(
        owner.telegram_id,
        "❌ Your booking on gpu-01 (Fri 06 10:00–Fri 06 14:00) was cancelled by an admin.\n"
        "Reason: maintenance",
    )]
    [a] = await _audit(db)
    assert (a.action, a.actor_user_id) == ("booking.cancel", admin.id)


async def test_system_cancel_dms_owner_and_audits_null_actor(db, store, booking):
    await _configure(store)
    await cancel_booking(db, store, booking.id, None, NOW)
    assert booking.status == "cancelled"
    [(_, text)] = await _texts(db)
    assert text == "❌ Your booking on gpu-01 (Fri 06 10:00–Fri 06 14:00) was cancelled by an admin."
    [a] = await _audit(db)
    assert (a.action, a.actor_user_id) == ("booking.cancel", None)


async def test_cancel_ongoing_ends_early(db, store, make_user, make_server):
    await _configure(store)
    user = await make_user()
    server = await make_server()
    b = await _add(db, user, server, at(11), at(17))
    await cancel_booking(db, store, b.id, user, NOW)
    assert b.status == "active" and b.end_at == at(13)
    assert len(await _items(db, "calendar_update")) == 1
    assert await _items(db, "calendar_delete") == []


async def test_extend_beyond_14_days_rejected(db, store, make_user, make_server):
    user = await make_user()
    server = await make_server()
    b = await _add(db, user, server, at(11), at(17))
    # From floor_hour(now)=12:00 this is exactly 336 h, but from the original start it is 337 h.
    with pytest.raises(BookingRejected) as e:
        await extend(db, store, b.id, user, at(11) + timedelta(hours=337), NOW)
    assert e.value.error == BookingError.TOO_LONG
    assert b.end_at == at(17)


async def test_extend_ongoing_ok_despite_past_start(db, store, make_user, make_server):
    await _configure(store)
    user = await make_user()
    server = await make_server()
    b = await _add(db, user, server, at(11), at(17))
    new_end = at(11) + timedelta(hours=336)
    await extend(db, store, b.id, user, new_end, NOW)
    assert b.end_at == new_end
    assert len(await _items(db, "calendar_update")) == 1
    [a] = await _audit(db)
    assert a.action == "booking.extend"


async def test_extend_into_other_booking_conflicts(db, store, make_user, make_server):
    ana, bob = await make_user(), await make_user()
    server = await make_server()
    b = await _add(db, ana, server, at(14), at(16))
    await _add(db, bob, server, at(17), at(19))
    with pytest.raises(BookingRejected) as e:
        await extend(db, store, b.id, ana, at(18), NOW)
    assert e.value.error == BookingError.CONFLICT
    await extend(db, store, b.id, ana, at(17), NOW)  # adjacent is fine
    assert b.end_at == at(17)


async def test_extend_keeps_existing_share_pair(db, store, make_user, make_server):
    ana, bob, cy = await make_user(), await make_user(), await make_user()
    server = await make_server(kind="grafana", shareable=True)
    a = await _add(db, ana, server, at(11), at(18))
    b = await _add(db, bob, server, at(12), at(14), is_shared=True)
    await _add(db, cy, server, at(20), at(22))
    await extend(db, store, a.id, ana, at(19), NOW)
    await extend(db, store, b.id, bob, at(16), NOW)
    assert (a.end_at, b.end_at) == (at(19), at(16))
    with pytest.raises(BookingRejected) as e:  # new overlap with a third booking
        await extend(db, store, b.id, bob, at(21), NOW)
    assert e.value.error == BookingError.CONFLICT


async def test_extend_must_be_later_and_aligned(db, store, make_user, make_server):
    user = await make_user()
    server = await make_server()
    b = await _add(db, user, server, at(14), at(16))
    with pytest.raises(BookingRejected) as e:
        await extend(db, store, b.id, user, at(16), NOW)
    assert e.value.error == BookingError.END_NOT_AFTER_START
    with pytest.raises(BookingRejected) as e:
        await extend(db, store, b.id, user, at(17) + timedelta(minutes=30), NOW)
    assert e.value.error == BookingError.NOT_HOUR_ALIGNED


async def test_extend_by_other_user_forbidden(db, store, make_user, make_server):
    ana, bob = await make_user(), await make_user()
    server = await make_server()
    b = await _add(db, ana, server, at(14), at(16))
    with pytest.raises(PermissionError):
        await extend(db, store, b.id, bob, at(18), NOW)


async def test_reclaim_sets_status_and_posts(db, store, make_user, make_server):
    await _configure(store)
    user = await make_user(display_name="Ana Rossi")
    server = await make_server(name="gpu-01")
    b = await _add(db, user, server, at(9), at(17))
    await reclaim(db, store, b.id, NOW)
    assert b.status == "reclaimed" and b.end_at == NOW
    assert len(await _items(db, "calendar_delete")) == 1
    assert await _texts(db) == [
        (user.telegram_id,
         "♻️ Your booking on gpu-01 was released because it was idle "
         "(no activity after 3 warnings)."),
        (GROUP, "♻️ gpu-01 booking by Ana Rossi was released after 3 idle warnings."),
    ]
    [a] = await _audit(db)
    assert (a.action, a.actor_user_id) == ("booking.reclaim", None)


async def test_acknowledge_still_using_only_owner(db, make_user, make_server):
    ana, bob = await make_user(), await make_user()
    server = await make_server()
    b = await _add(db, ana, server, at(9), at(17), idle_warnings=2, idle_since=at(10),
                   idle_ram_baseline=40.0)
    assert await acknowledge_still_using(db, b.id, bob.telegram_id, NOW) is False
    assert b.idle_warnings == 2
    assert await acknowledge_still_using(db, b.id, ana.telegram_id, NOW) is True
    assert (b.idle_warnings, b.idle_since, b.idle_ram_baseline) == (0, None, None)


async def test_user_bookings_active_not_ended(db, make_user, make_server):
    ana, bob = await make_user(display_name="ana"), await make_user()
    server = await make_server()
    await _add(db, ana, server, at(8), at(10))  # ended
    later = await _add(db, ana, server, at(15), at(17))
    now_b = await _add(db, ana, server, at(11), at(13))
    await _add(db, ana, server, at(18), at(19), status="cancelled")
    await _add(db, bob, server, at(19), at(20))
    views = await user_bookings(db, ana.id, NOW)
    assert [v.id for v in views] == [now_b.id, later.id]
    assert views[0].user_name == "ana"
