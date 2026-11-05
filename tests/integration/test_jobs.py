import logging
from datetime import datetime, timedelta

import pytest_asyncio
from sqlalchemy import select

from app.domain.idle import IdleAction
from app.domain.timeutil import parse_local
from app.jobs.lifecycle import booking_lifecycle
from app.jobs.membership import membership_sync
from app.jobs.monitor import idle_check, spike_check
from app.jobs.runner import run_job
from app.models import Booking, MetricSnapshot, OutboxItem, Server, User
from app.services import watchlist
from app.settings_store import K
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


async def _snap(db, server_id, ts, cpu=1.0, ram=30.0, gpus=((0.0, 1.0),)):
    db.add(MetricSnapshot(
        server_id=server_id, ts=ts, cpu_pct=cpu, ram_pct=ram,
        gpus=[{"util_pct": u, "mem_pct": m} for u, m in gpus],
    ))
    await db.flush()


async def _book(db, user, server, start, end, resource="cpu_gpu", is_shared=False):
    b = Booking(
        user_id=user.id, server_id=server.id, start_at=start, end_at=end,
        resource=resource, is_shared=is_shared,
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


async def test_idle_job_skips_shared_servers(db, store, make_user, quiet_booking, gpu_server):
    other = await make_user(display_name="Bea")
    shared = await _book(
        db, other, gpu_server, L("2026-11-10T09:00"), L("2026-11-10T19:00"), is_shared=True
    )
    actions = await _drive_idle(
        db, store, gpu_server.id, L("2026-11-10T09:00"), L("2026-11-10T17:05")
    )
    assert actions == []
    assert await _items(db) == []
    for b in (quiet_booking, shared):
        await db.refresh(b)
        assert b.status == "active" and b.idle_since is None and b.idle_warnings == 0


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


# --- spike --------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def shared_pair(db, make_user, gpu_server):
    a = await make_user(display_name="Ana")
    b = await make_user(display_name="Bea")
    first = await _book(db, a, gpu_server, L("2026-11-10T09:00"), L("2026-11-10T19:00"))
    second = await _book(
        db, b, gpu_server, L("2026-11-10T10:00"), L("2026-11-10T14:00"), is_shared=True
    )
    return gpu_server, (a, first), (b, second)


async def test_spike_alert_once_per_spike(db, store, shared_pair):
    server, (ana, _first), (bea, second) = shared_pair
    now = L("2026-11-10T10:30")
    # Quiet: no alert.
    await _snap(db, server.id, now - STEP)
    assert await spike_check(db, store, now) == 0
    # Spike starts: alert once, to both users.
    now += STEP
    await _snap(db, server.id, now, cpu=100.0)
    assert await spike_check(db, store, now) == 1
    assert await _texts(db, ana.telegram_id) == [
        "⚠️ Usage on gpu-01 is high. You are sharing it with Bea."
    ]
    assert await _texts(db, bea.telegram_id) == [
        "⚠️ Usage on gpu-01 is high. You are sharing it with Ana."
    ]
    await db.refresh(second)
    assert second.spike_alerted is True
    # Still high: no repeat.
    now += STEP
    await _snap(db, server.id, now, cpu=100.0)
    assert await spike_check(db, store, now) == 0
    assert len(await _items(db)) == 2
    # Spike ends (15-min average drops back under threshold): flag cleared.
    now += timedelta(minutes=20)
    await _snap(db, server.id, now)
    assert await spike_check(db, store, now) == 0
    await db.refresh(second)
    assert second.spike_alerted is False
    # A new spike alerts again.
    now += STEP
    await _snap(db, server.id, now, cpu=100.0)
    assert await spike_check(db, store, now) == 1
    assert len(await _items(db)) == 4


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


async def test_shareable_flapping_does_not_post_freed(db, store, make_user, make_server):
    await _configure_topic(store)
    owner = await make_user()
    watcher = await make_user()
    server = await make_server(
        name="gpu-05", kind="grafana", has_gpu=True, gpu_count=1, shareable=True
    )
    await _book(db, owner, server, L("2026-11-10T09:00"), L("2026-11-10T19:00"))
    assert await watchlist.subscribe(db, watcher.id, server.id, L("2026-11-10T09:00"))

    now = L("2026-11-10T09:30")
    codes = []
    for busy in (True, False, True, False):
        cpu = 90.0 if busy else 5.0
        await _snap(db, server.id, now, cpu=cpu)
        await booking_lifecycle(db, store, now)
        srv = await db.get(Server, server.id)
        await db.refresh(srv)
        codes.append(srv.last_status_code)
        now += timedelta(minutes=90)  # beyond the 60-min share window
    assert codes == ["booked", "shareable", "booked", "shareable"]
    assert await _topic_texts(db) == []
    assert len(await _texts(db, watcher.telegram_id)) == 1


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
