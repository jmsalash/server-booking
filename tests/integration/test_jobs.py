import logging
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.domain.idle import IdleAction
from app.domain.timeutil import parse_local
from app.jobs.lifecycle import booking_lifecycle
from app.jobs.membership import membership_sync
from app.jobs.monitor import full_check, idle_check
from app.jobs.runner import run_job
from app.models import Booking, MetricSnapshot, OutboxItem, Server, User
from app.services import booking as booking_svc
from app.services import watchlist
from app.settings_store import K, SettingsStore
from tests.fakes import FakeTelegram

TZ = "Europe/Rome"
STEP = timedelta(minutes=5)


def L(value: str) -> datetime:
    return parse_local(value, TZ)


async def _items(db) -> list[OutboxItem]:
    return list((await db.scalars(select(OutboxItem).order_by(OutboxItem.id))).all())


async def _texts(db, chat_id=None) -> list[str]:
    return [
        it.payload["text"] for it in await _items(db)
        if chat_id is None or it.payload["chat_id"] == chat_id
    ]


async def _topic_texts(db) -> list[str]:
    return [it.payload["text"] for it in await _items(db) if it.payload["thread_id"] is not None]


async def _snap(db, server_id, ts, cpu=1.0, ram=30.0, gpus=((0.0, 1.0),), disks=()):
    db.add(MetricSnapshot(
        server_id=server_id, ts=ts, cpu_pct=cpu, ram_pct=ram,
        gpus=[{"util_pct": u, "mem_pct": m} for u, m in gpus],
        disks=[{"mount": mnt, "used_pct": pct} for mnt, pct in disks],
    ))
    await db.flush()


async def _book(db, user, server, start, end, resource="cpu_gpu", is_shared=False,
                shareable=False):
    b = Booking(
        user_id=user.id, server_id=server.id, start_at=start, end_at=end,
        resource=resource, is_shared=is_shared, shareable=shareable,
    )
    db.add(b)
    await db.flush()
    return b


async def _drive_idle(db, store, server_id, start, end, snap_until=None):
    """Call idle_check every 5 min in [start, end], inserting a quiet snapshot each tick."""
    actions = []
    now = start
    while now <= end:
        if snap_until is None or now <= snap_until:
            await _snap(db, server_id, now)
        for bid, action in await idle_check(db, store, now):
            actions.append((now, bid, action))
        now += STEP
    return actions


@pytest_asyncio.fixture
async def gpu_server(make_server):
    return await make_server(name="gpu-01", kind="grafana", has_gpu=True, gpu_count=1)


@pytest_asyncio.fixture
async def quiet_booking(db, make_user, gpu_server):
    user = await make_user(display_name="Ana Rossi")
    # Tue 2026-11-10, whole working day.
    return await _book(db, user, gpu_server, L("2026-11-10T09:00"), L("2026-11-10T19:00"))


async def _configure_topic(store):
    await store.set(K.TG_GROUP_ID, -100123)
    await store.set(K.TG_TOPIC_ID, 7)
    await store.set(K.PORTAL_BASE_URL, "https://portal.lab/")


# --- idle ---------------------------------------------------------------------------------------


async def test_idle_job_warns_with_button(db, store, quiet_booking):
    actions = await _drive_idle(
        db, store, quiet_booking.server_id, L("2026-11-10T09:00"), L("2026-11-10T11:00")
    )
    assert actions == [(L("2026-11-10T11:00"), quiet_booking.id, IdleAction.WARN)]
    [item] = await _items(db)
    owner = await db.get(User, quiet_booking.user_id)
    assert item.payload == {
        "chat_id": owner.telegram_id,
        "text": "⚠️ Your booking on gpu-01 has been idle for 2 hours. "
                "Warning 1/3 — after 3 warnings the booking is released.",
        "thread_id": None,
        "buttons": [["I'm still using it", f"still:{quiet_booking.id}"]],
    }
    await db.refresh(quiet_booking)
    assert quiet_booking.idle_warnings == 1
    assert quiet_booking.idle_since == L("2026-11-10T11:00")
    assert quiet_booking.idle_ram_baseline == 30.0
    assert quiet_booking.last_warning_at == L("2026-11-10T11:00")


async def test_idle_job_reclaims_after_three_warnings(db, store, quiet_booking):
    await _configure_topic(store)
    actions = await _drive_idle(
        db, store, quiet_booking.server_id, L("2026-11-10T09:00"), L("2026-11-10T17:05")
    )
    assert [(t, a) for t, _, a in actions] == [
        (L("2026-11-10T11:00"), IdleAction.WARN),
        (L("2026-11-10T13:00"), IdleAction.WARN),
        (L("2026-11-10T15:00"), IdleAction.WARN),
        (L("2026-11-10T17:00"), IdleAction.RECLAIM),
    ]
    await db.refresh(quiet_booking)
    assert quiet_booking.status == "reclaimed"
    assert quiet_booking.end_at == L("2026-11-10T17:00")
    texts = await _texts(db)
    assert [t.split("Warning ")[1][:3] for t in texts if "Warning" in t] == ["1/3", "2/3", "3/3"]
    assert any(t.startswith("♻️ Your booking on gpu-01") for t in texts)


async def _partner(db, make_user, server, start="2026-11-10T09:00", end="2026-11-10T19:00"):
    other = await make_user(display_name="Bea")
    return other, await _book(db, other, server, L(start), L(end), resource="cpu", is_shared=True)


async def test_idle_shared_machine_warns_both(db, store, make_user, quiet_booking, gpu_server):
    bea, partner = await _partner(db, make_user, gpu_server)
    actions = await _drive_idle(
        db, store, gpu_server.id, L("2026-11-10T09:00"), L("2026-11-10T11:00")
    )
    assert sorted(actions) == sorted([
        (L("2026-11-10T11:00"), quiet_booking.id, IdleAction.WARN),
        (L("2026-11-10T11:00"), partner.id, IdleAction.WARN),
    ])
    ana = await db.get(User, quiet_booking.user_id)
    buttons = {it.payload["chat_id"]: it.payload["buttons"] for it in await _items(db)}
    assert buttons == {
        ana.telegram_id: [["I'm still using it", f"still:{quiet_booking.id}"]],
        bea.telegram_id: [["I'm still using it", f"still:{partner.id}"]],
    }


async def test_idle_shared_reclaims_only_the_non_responder(db, store, make_user, quiet_booking,
                                                           gpu_server):
    bea, partner = await _partner(db, make_user, gpu_server)
    now, end = L("2026-11-10T09:00"), L("2026-11-10T17:05")
    actions = []
    while now <= end:
        await _snap(db, gpu_server.id, now)
        for bid, action in await idle_check(db, store, now):
            actions.append((now, bid, action))
            if bid == partner.id and action == IdleAction.WARN:  # Bea answers every warning
                assert await booking_svc.acknowledge_still_using(db, partner.id,
                                                                 bea.telegram_id, now)
        now += STEP
    assert [(t, a) for t, bid, a in actions if bid == quiet_booking.id][-1] == (
        L("2026-11-10T17:00"), IdleAction.RECLAIM)
    assert IdleAction.RECLAIM not in [a for _, bid, a in actions if bid == partner.id]
    await db.refresh(quiet_booking)
    await db.refresh(partner)
    assert quiet_booking.status == "reclaimed" and partner.status == "active"


async def test_idle_shared_activity_resets_both(db, store, make_user, quiet_booking, gpu_server):
    _, partner = await _partner(db, make_user, gpu_server)
    await _drive_idle(db, store, gpu_server.id, L("2026-11-10T09:00"), L("2026-11-10T11:00"))
    for b in (quiet_booking, partner):
        await db.refresh(b)
        assert b.idle_warnings == 1
    busy = L("2026-11-10T11:05")
    await _snap(db, gpu_server.id, busy, cpu=90.0)
    assert await idle_check(db, store, busy) == []
    for b in (quiet_booking, partner):
        await db.refresh(b)
        assert (b.idle_warnings, b.idle_since) == (0, None)


async def test_idle_busy_gpu_keeps_cpu_only_booking_alive(db, store, make_user, gpu_server):
    ana = await make_user()
    await _book(db, ana, gpu_server, L("2026-11-10T09:00"), L("2026-11-10T19:00"), "cpu",
                shareable=True)
    _, partner = await _partner(db, make_user, gpu_server)
    partner.shareable, partner.resource = False, "cpu_gpu"
    now, actions = L("2026-11-10T09:00"), []
    while now <= L("2026-11-10T13:00"):
        await _snap(db, gpu_server.id, now, gpus=((90.0, 50.0),))  # GPU busy, CPU quiet
        actions += await idle_check(db, store, now)
        now += STEP
    assert actions == []


async def test_idle_job_skips_standalone(db, store, make_user, make_server):
    user = await make_user()
    server = await make_server(kind="standalone")
    b = await _book(db, user, server, L("2026-11-10T09:00"), L("2026-11-10T19:00"), "cpu")
    actions = await _drive_idle(db, store, server.id, L("2026-11-10T09:00"), L("2026-11-10T17:05"))
    assert actions == []
    assert await _items(db) == []
    await db.refresh(b)
    assert b.status == "active" and b.idle_since is None


async def test_grafana_outage_pauses_reclaim(db, store, quiet_booking):
    last_snap = L("2026-11-10T10:00")
    actions = await _drive_idle(
        db, store, quiet_booking.server_id, L("2026-11-10T09:00"), L("2026-11-10T18:00"),
        snap_until=last_snap,
    )
    cutoff = last_snap + timedelta(minutes=15)
    assert not [t for t, _, a in actions if a == IdleAction.WARN and t > cutoff]
    assert not [a for _, _, a in actions if a == IdleAction.RECLAIM]
    await db.refresh(quiet_booking)
    assert quiet_booking.status == "active"
    assert quiet_booking.idle_since is None


# --- full RAM / disk ----------------------------------------------------------------------------


@pytest_asyncio.fixture
async def shared_pair(db, make_user, gpu_server):
    a = await make_user(display_name="Ana")
    b = await make_user(display_name="Bea")
    first = await _book(db, a, gpu_server, L("2026-11-10T09:00"), L("2026-11-10T19:00"),
                        shareable=True)
    second = await _book(
        db, b, gpu_server, L("2026-11-10T10:00"), L("2026-11-10T14:00"), is_shared=True
    )
    return gpu_server, (a, first), (b, second)


RAM_TEXT = "⚠️ gpu-01: RAM is {}% full. Your job may crash if it runs out."


async def test_full_ram_alerts_both_once_and_rearms(db, store, shared_pair):
    await _configure_topic(store)
    server, (ana, first), (bea, second) = shared_pair
    now = L("2026-11-10T10:30")
    await _snap(db, server.id, now, ram=80.0)
    assert await full_check(db, store, now) == 0
    # Crosses 90 %: both people get it, nothing in the topic.
    now += STEP
    await _snap(db, server.id, now, ram=94.0)
    assert await full_check(db, store, now) == 2
    assert await _texts(db, ana.telegram_id) == [RAM_TEXT.format(94)]
    assert await _texts(db, bea.telegram_id) == [RAM_TEXT.format(94)]
    assert await _topic_texts(db) == []
    await db.refresh(first)
    assert first.ram_alerted_at == now
    # Still high, then down to 86 (not 5 pp below): no repeat.
    for ram in (97.0, 86.0, 93.0):
        now += STEP
        await _snap(db, server.id, now, ram=ram)
        assert await full_check(db, store, now) == 0, ram
    # Below 85: re-armed; crossing again alerts again.
    now += STEP
    await _snap(db, server.id, now, ram=84.0)
    assert await full_check(db, store, now) == 0
    await db.refresh(first)
    assert first.ram_alerted_at is None
    now += STEP
    await _snap(db, server.id, now, ram=91.0)
    assert await full_check(db, store, now) == 2
    assert len(await _items(db)) == 4


async def test_full_disk_lists_mounts_at_night(db, store, make_user, gpu_server):
    ana = await make_user(display_name="Ana")
    b = await _book(db, ana, gpu_server, L("2026-11-10T20:00"), L("2026-11-11T08:00"))
    now = L("2026-11-11T03:00")  # outside working hours
    await _snap(db, gpu_server.id, now, disks=[("/", 41.0), ("/data", 80.0), ("/scratch", 70.0)])
    assert await full_check(db, store, now) == 0  # the baseline: nothing full
    await db.refresh(b)
    assert b.disk_alerted == []
    now += STEP
    await _snap(db, gpu_server.id, now,
                disks=[("/", 41.0), ("/data", 96.0), ("/scratch", 92.4)])
    assert await full_check(db, store, now) == 1
    assert await _texts(db, ana.telegram_id) == [
        "⚠️ gpu-01: disks are almost full: /data 96%, /scratch 92%. "
        "Free some space before they run out."
    ]
    await db.refresh(b)
    assert b.disk_alerted == ["/data", "/scratch"] and b.ram_alerted_at is None
    now += STEP
    await _snap(db, gpu_server.id, now, disks=[("/", 91.0), ("/data", 96.0)])
    assert await full_check(db, store, now) == 1
    assert (await _texts(db, ana.telegram_id))[-1] == (
        "⚠️ gpu-01: disk / is 91% full. Free some space before it runs out."
    )


async def test_full_not_for_free_machines_or_stale_data(db, store, make_user, gpu_server,
                                                       make_server):
    ana = await make_user(display_name="Ana")
    now = L("2026-11-10T10:30")
    # Free machine with full RAM/disk: nobody is told.
    await _snap(db, gpu_server.id, now, ram=99.0, disks=[("/", 99.0)])
    assert await full_check(db, store, now) == 0
    # In use, but the last reading is stale: no alert and nothing remembered.
    other = await make_server(name="gpu-02", kind="grafana")
    b = await _book(db, ana, other, L("2026-11-10T09:00"), L("2026-11-10T19:00"))
    await _snap(db, other.id, now - timedelta(minutes=30), ram=99.0, disks=[("/", 99.0)])
    assert await full_check(db, store, now) == 0
    await db.refresh(b)
    assert b.ram_alerted_at is None and b.disk_alerted is None
    assert await _items(db) == []


async def test_disk_already_full_at_start_warns_only_after_refilling(db, store, make_user,
                                                                      gpu_server):
    ana = await make_user(display_name="Ana")
    b = await _book(db, ana, gpu_server, L("2026-11-10T09:00"), L("2026-11-10T19:00"))
    assert b.disk_alerted is None
    now = L("2026-11-10T09:05")
    await _snap(db, gpu_server.id, now, disks=[("/", 41.0), ("/data", 96.0)])
    assert await full_check(db, store, now) == 0  # already full when the booking began
    await db.refresh(b)
    assert b.disk_alerted == ["/data"]
    for pct in (97.0, 86.0, 92.0):  # still full / not yet 5 pp below: nothing
        now += STEP
        await _snap(db, gpu_server.id, now, disks=[("/", 41.0), ("/data", pct)])
        assert await full_check(db, store, now) == 0, pct
    now += STEP
    await _snap(db, gpu_server.id, now, disks=[("/", 41.0), ("/data", 84.0)])
    assert await full_check(db, store, now) == 0
    now += STEP
    await _snap(db, gpu_server.id, now, disks=[("/", 41.0), ("/data", 91.0)])
    assert await full_check(db, store, now) == 1
    assert await _texts(db, ana.telegram_id) == [
        "⚠️ gpu-01: disk /data is 91% full. Free some space before it runs out."
    ]


async def test_disk_baseline_waits_for_disk_data(db, store, make_user, gpu_server):
    ana = await make_user(display_name="Ana")
    b = await _book(db, ana, gpu_server, L("2026-11-10T09:00"), L("2026-11-10T19:00"))
    now = L("2026-11-10T09:05")
    await _snap(db, gpu_server.id, now)  # disk query failed: no disks
    assert await full_check(db, store, now) == 0
    await db.refresh(b)
    assert b.disk_alerted is None
    now += STEP
    await _snap(db, gpu_server.id, now, disks=[("/", 95.0)])
    assert await full_check(db, store, now) == 0  # first disk data is the baseline
    await db.refresh(b)
    assert b.disk_alerted == ["/"]


async def test_full_check_locks_only_changed_bookings(sessionmaker, box, shared_pair, db):
    server, (_ana, first), (_bea, second) = shared_pair
    now = L("2026-11-10T10:30")
    # The partner was already warned; the main booking was not.
    second.ram_alerted_at = now - STEP
    await _snap(db, server.id, now, ram=95.0)
    await db.commit()
    async with sessionmaker() as holder, sessionmaker() as s:
        # Another transaction holds the partner's row: full_check must not wait for it.
        await holder.execute(select(Booking).where(Booking.id == second.id).with_for_update())
        await s.execute(text("SET LOCAL lock_timeout = '2s'"))
        assert await full_check(s, SettingsStore(s, box), now) == 1
        await s.rollback()
        await holder.rollback()


# --- lifecycle ----------------------------------------------------------------------------------


async def test_completion_and_reminders_sent_once(db, store, make_user, make_server):
    await store.set(K.PORTAL_BASE_URL, "https://portal.lab/")
    user = await make_user()
    server = await make_server(name="cpu-07")
    b = await _book(db, user, server, L("2026-11-10T10:00"), L("2026-11-10T12:00"), "cpu")
    # A booking whose start already passed never gets a start reminder.
    late = await _book(
        db, user, await make_server(name="cpu-08"),
        L("2026-11-10T09:00"), L("2026-11-10T18:00"), "cpu",
    )

    async def run(at):
        await booking_lifecycle(db, store, L(at))
        return await _texts(db, user.telegram_id)

    assert await run("2026-11-10T09:29") == []
    start_msg = "⏰ Your booking on cpu-07 starts at 10:00."
    assert await run("2026-11-10T09:30") == [start_msg]
    assert await run("2026-11-10T09:45") == [start_msg]
    end_msg = "⏰ Your booking on cpu-07 ends at 12:00. Extend: https://portal.lab/me"
    assert await run("2026-11-10T11:00") == [start_msg, end_msg]
    assert await run("2026-11-10T11:30") == [start_msg, end_msg]
    await db.refresh(b)
    assert b.status == "active" and b.start_reminder_sent and b.end_reminder_sent
    await run("2026-11-10T12:00")
    await db.refresh(b)
    assert b.status == "completed"
    await db.refresh(late)
    assert late.status == "active" and not late.start_reminder_sent


async def test_extend_rearms_end_reminder(db, store, make_user, make_server):
    user = await make_user()
    server = await make_server(name="cpu-07")
    b = await _book(db, user, server, L("2026-11-10T10:00"), L("2026-11-10T12:00"), "cpu")
    await booking_lifecycle(db, store, L("2026-11-10T11:00"))
    assert await _texts(db, user.telegram_id) == ["⏰ Your booking on cpu-07 ends at 12:00."]
    await booking_svc.extend(db, store, b.id, user, L("2026-11-10T15:00"), L("2026-11-10T11:05"))
    assert b.end_reminder_sent is False
    await booking_lifecycle(db, store, L("2026-11-10T14:00"))
    assert await _texts(db, user.telegram_id) == [
        "⏰ Your booking on cpu-07 ends at 12:00.",
        "⏰ Your booking on cpu-07 ends at 15:00.",
    ]


async def test_end_early_suppresses_end_reminder(db, store, make_user, make_server):
    user = await make_user()
    server = await make_server(name="cpu-07")
    b = await _book(db, user, server, L("2026-11-10T09:00"), L("2026-11-10T18:00"), "cpu")
    await booking_svc.end_early(db, store, b.id, user, L("2026-11-10T10:20"))
    assert b.end_at == L("2026-11-10T10:20") and b.status == "completed"
    await booking_lifecycle(db, store, L("2026-11-10T10:21"))
    assert await _texts(db, user.telegram_id) == []


async def test_freed_transition_posts_topic_and_fires_watchlist(db, store, make_user, make_server):
    await _configure_topic(store)
    owner = await make_user()
    watcher = await make_user()
    server = await make_server(name="cpu-09")
    b = await _book(db, owner, server, L("2026-11-10T09:00"), L("2026-11-10T12:00"), "cpu")

    await booking_lifecycle(db, store, L("2026-11-10T10:00"))  # first run: free soon, no posts
    srv = await db.get(Server, server.id)
    assert srv.last_status_code == "free_soon"
    assert await _items(db) == []

    assert await watchlist.subscribe(db, watcher.id, server.id, L("2026-11-10T10:00"))
    await booking_lifecycle(db, store, L("2026-11-10T12:00"))
    await db.refresh(b)
    await db.refresh(srv)
    assert b.status == "completed"
    assert srv.last_status_code == "available"
    assert await _topic_texts(db) == ["🟢 cpu-09 is now free."]
    [dm] = await _texts(db, watcher.telegram_id)
    assert dm.startswith("🟢 cpu-09 is now Available.")
    assert await watchlist.for_user(db, watcher.id) == []

    # Steady state: nothing more.
    n = len(await _items(db))
    await booking_lifecycle(db, store, L("2026-11-10T12:05"))
    assert len(await _items(db)) == n


async def test_partner_joining_and_leaving_does_not_post_freed(db, store, make_user,
                                                               make_server):
    await _configure_topic(store)
    owner, bea, watcher = await make_user(), await make_user(), await make_user()
    server = await make_server(name="gpu-05", kind="grafana", has_gpu=True, gpu_count=1)
    await _book(db, owner, server, L("2026-11-10T09:00"), L("2026-11-10T19:00"), shareable=True)
    await _book(db, bea, server, L("2026-11-10T10:00"), L("2026-11-10T11:00"), is_shared=True)
    assert await watchlist.subscribe(db, watcher.id, server.id, L("2026-11-10T09:00"))
    codes = []
    for t in ("09:30", "10:30", "11:30"):
        await booking_lifecycle(db, store, L(f"2026-11-10T{t}"))
        srv = await db.get(Server, server.id)
        await db.refresh(srv)
        codes.append(srv.last_status_code)
    assert codes == ["shareable", "booked", "shareable"]
    assert await _topic_texts(db) == []
    # "One more person can join" is not free: the watcher hears nothing and stays subscribed.
    assert await _texts(db, watcher.telegram_id) == []
    assert len(await watchlist.for_user(db, watcher.id)) == 1


async def test_watcher_told_only_when_completely_free(db, store, make_user, make_server):
    await _configure_topic(store)
    owner, bea, watcher = await make_user(), await make_user(), await make_user()
    server = await make_server(name="gpu-07", kind="grafana", has_gpu=True, gpu_count=1)
    await _book(db, owner, server, L("2026-11-10T09:00"), L("2026-11-10T12:00"), shareable=True)
    await _book(db, bea, server, L("2026-11-10T09:00"), L("2026-11-10T10:00"), is_shared=True)
    await booking_lifecycle(db, store, L("2026-11-10T09:30"))  # booked (shared)
    assert await watchlist.subscribe(db, watcher.id, server.id, L("2026-11-10T09:30"))
    await booking_lifecycle(db, store, L("2026-11-10T10:30"))  # partner left: shareable
    assert await _texts(db, watcher.telegram_id) == []
    await booking_lifecycle(db, store, L("2026-11-10T12:00"))  # owner done: free
    assert await _texts(db, watcher.telegram_id) == [
        f"🟢 gpu-07 is now Available. Book: https://portal.lab/book?server_id={server.id}"
    ]
    assert await watchlist.for_user(db, watcher.id) == []
    assert await _topic_texts(db) == ["🟢 gpu-07 is now free."]


async def test_shareable_to_available_posts_freed_once(db, store, make_user, make_server):
    await _configure_topic(store)
    owner = await make_user()
    server = await make_server(name="gpu-06", kind="grafana", has_gpu=True, gpu_count=1)
    await _book(db, owner, server, L("2026-11-10T09:00"), L("2026-11-10T12:00"), shareable=True)
    await booking_lifecycle(db, store, L("2026-11-10T09:30"))
    srv = await db.get(Server, server.id)
    assert srv.last_status_code == "shareable"
    await booking_lifecycle(db, store, L("2026-11-10T12:00"))
    await db.refresh(srv)
    assert srv.last_status_code == "available"
    await booking_lifecycle(db, store, L("2026-11-10T12:05"))
    assert await _topic_texts(db) == ["🟢 gpu-06 is now free."]


async def test_lifecycle_locks_servers_before_bookings(sessionmaker, box, make_user, make_server,
                                                        db, store):
    user = await make_user()
    server = await make_server(name="cpu-10")
    await _book(db, user, server, L("2026-11-10T10:00"), L("2026-11-10T12:00"), "cpu")
    await booking_lifecycle(db, store, L("2026-11-10T10:50"))  # status settles: free soon
    await db.commit()
    async with sessionmaker() as job, sessionmaker() as other:
        # Status unchanged, so only the end reminder (a booking row) is written.
        await booking_lifecycle(job, SettingsStore(job, box), L("2026-11-10T11:00"))
        # The job (uncommitted) holds the server row, so a booking write path that locks the
        # server first waits here instead of deadlocking on a booking row later.
        with pytest.raises(DBAPIError) as e:
            await other.execute(
                select(Server).where(Server.id == server.id).with_for_update(nowait=True)
            )
        assert "55P03" in str(e.value.orig.sqlstate)
        await other.rollback()
        await job.rollback()


# --- membership ---------------------------------------------------------------------------------


async def test_membership_sync_noop_without_group(db, store, make_user):
    u = await make_user()
    assert await membership_sync(db, store, FakeTelegram(), L("2026-11-10T10:00")) is None
    await db.refresh(u)
    assert u.active


async def test_membership_sync_wraps_auth(db, store, make_user):
    await store.set(K.TG_GROUP_ID, -100123)
    u = await make_user()
    result = await membership_sync(db, store, FakeTelegram(), L("2026-11-10T10:00"))
    assert result.deactivated == [u.id]


# --- runner -------------------------------------------------------------------------------------


async def test_run_job_swallows_exceptions(sessionmaker, caplog):
    async def boom(db, now):
        db.add(User(telegram_id=1, display_name="ghost"))
        await db.flush()
        raise RuntimeError("kaput")

    async def ok(db, now):
        db.add(User(telegram_id=2, display_name="real"))
        await db.flush()

    with caplog.at_level(logging.ERROR):
        await run_job("boom", boom, sessionmaker)
    assert "job boom failed" in caplog.text
    assert "kaput" in caplog.text  # traceback logged

    await run_job("ok", ok, sessionmaker)
    async with sessionmaker() as db:
        names = (await db.scalars(select(User.display_name))).all()
    assert names == ["real"]


async def test_run_job_survives_failing_rollback(caplog):
    class BrokenSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            raise AssertionError("not reached")

        async def rollback(self):
            raise ConnectionError("connection lost")

    async def boom(db, now):
        raise RuntimeError("kaput")

    with caplog.at_level(logging.ERROR):
        await run_job("boom", boom, lambda: BrokenSession())
    assert "job boom failed" in caplog.text
    assert "rollback after job boom failed" in caplog.text


# --- maintenance --------------------------------------------------------------------------------


async def test_prune_housekeeping(db, store, make_user):
    from app.jobs.maintenance import prune_housekeeping
    from app.models import LoginToken, Session

    now = L("2026-11-10T10:00")
    user = await make_user()
    db.add_all([
        LoginToken(token="old", expires_at=now - timedelta(days=1, minutes=1)),
        LoginToken(token="recent", expires_at=now - timedelta(hours=23)),
        Session(id="gone", user_id=user.id, expires_at=now - timedelta(seconds=1)),
        Session(id="live", user_id=user.id, expires_at=now + timedelta(days=1)),
        OutboxItem(kind="k", payload={}, done_at=now - timedelta(days=7, minutes=1)),
        OutboxItem(kind="k", payload={}, done_at=now - timedelta(days=6)),
        OutboxItem(kind="k", payload={}, done_at=None, created_at=now - timedelta(days=30)),
    ])
    await db.flush()
    counts = await prune_housekeeping(db, store, now)
    assert counts == {"login_tokens": 1, "sessions": 1, "outbox_items": 1}
    assert (await db.scalars(select(LoginToken.token))).all() == ["recent"]
    assert (await db.scalars(select(Session.id))).all() == ["live"]
    left = (await db.scalars(select(OutboxItem.done_at).order_by(OutboxItem.id))).all()
    assert left == [now - timedelta(days=6), None]


async def test_idle_job_ignores_archived_servers(db, store, quiet_booking, gpu_server):
    # enabled left on on purpose: archived_at alone must stop idle tracking and reclaim
    gpu_server.archived_at = L("2026-11-10T08:00")
    await db.flush()
    actions = await _drive_idle(db, store, gpu_server.id, L("2026-11-10T09:00"),
                                L("2026-11-10T12:00"))
    assert actions == []
    assert (await db.get(Booking, quiet_booking.id)).idle_warnings == 0
