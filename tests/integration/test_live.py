import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.models import Booking
from app.web import live
from tests.integration.test_portal import login_as

START = datetime(2026, 11, 6, 9, tzinfo=UTC)


async def _signal(q, timeout=2.0) -> bool:
    try:
        await asyncio.wait_for(q.get(), timeout)
        return True
    except TimeoutError:
        return False


@pytest.fixture
async def listening(sessionmaker):
    hub = live.LiveHub()
    q = hub.subscribe()
    task = asyncio.create_task(live.listen(sessionmaker.kw["bind"], hub))
    assert await _signal(q)  # connected (a connect always signals once)
    yield q
    task.cancel()


async def test_committed_booking_changes_reach_open_pages(listening, db, make_user,
                                                          make_server):
    q = listening
    user, server = await make_user(), await make_server()
    b = Booking(user_id=user.id, server_id=server.id, start_at=START,
                end_at=START + timedelta(hours=2), resource="cpu")
    db.add(b)
    await db.commit()
    assert await _signal(q)

    b.end_at = START + timedelta(hours=3)
    await db.commit()
    assert await _signal(q)

    # Bookkeeping the pages don't show stays quiet.
    b.idle_warnings = 1
    b.end_reminder_sent = True
    await db.commit()
    assert not await _signal(q, 0.5)

    # Nothing is announced for a change that is rolled back.
    b.status = "cancelled"
    await db.flush()
    await db.rollback()
    assert not await _signal(q, 0.5)


async def test_server_status_change_is_announced(listening, db, make_server):
    q = listening
    server = await make_server()
    await db.commit()
    assert await _signal(q)
    server.last_status_code = "booked"
    await db.commit()
    assert await _signal(q)
    server.notes = "only an admin note"
    await db.commit()
    assert not await _signal(q, 0.5)


async def test_hub_collapses_bursts_and_forgets_closed_streams():
    hub = live.LiveHub()
    events = live._events(hub)
    assert await anext(events) == "retry: 5000\n\n"
    step = asyncio.ensure_future(anext(events))
    await asyncio.sleep(0)  # subscribed and waiting
    assert len(hub) == 1
    for _ in range(5):
        hub.publish()
    assert await step == "data: changed\n\n"
    step = asyncio.ensure_future(anext(events))
    await asyncio.sleep(0)
    assert step.done() is False  # the five signals became one refresh
    step.cancel()  # the page went away
    with pytest.raises(asyncio.CancelledError):
        await step
    await events.aclose()
    assert len(hub) == 0


async def test_closing_ends_open_streams():
    hub = live.LiveHub()
    events = live._events(hub)
    await anext(events)
    step = asyncio.ensure_future(anext(events))
    await asyncio.sleep(0)
    hub.publish()  # a pending change doesn't hold the stream open
    hub.close()
    with pytest.raises(StopAsyncIteration):
        await step
    assert len(hub) == 0


async def test_live_stream_needs_login(client):
    assert (await client.get("/live")).status_code == 401


async def test_board_and_calendar_refresh_on_live_signal(client, db, web_app, make_user,
                                                         make_server):
    await login_as(db, client, web_app, await make_user())
    await make_server(name="srv")
    await db.commit()
    board = (await client.get("/")).text
    assert 'id="board-cards" data-live' in board and 'hx-trigger="every 60s, live"' in board
    cal = (await client.get("/calendar?week=2026-11-09")).text
    assert 'id="cal-body" data-live hx-get="/calendar?week=2026-11-09"' in cal
    assert 'hx-select="#cal-body"' in cal
