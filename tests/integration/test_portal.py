import logging
import re
import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import func, select

import app.clock
from app.domain.booking_rules import BookingError
from app.domain.types import ServerUsage
from app.integrations.telegram_gateway import MemberStatus
from app.domain.rules import Rules
from app.models import Booking, LoginToken, OutboxItem, Session, WatchSubscription
from app.services import auth, usage
from app.settings_store import K
from app.web.portal import ERROR_TEXT, error_text, max_length_text
from tests.fakes import FakeTelegram

NOW = datetime(2026, 11, 9, 10, 20, tzinfo=UTC)  # Mon 09, 11:20 Europe/Rome
GROUP = -100123


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(app.clock, "utcnow", lambda: state["now"])
    return state


async def login_as(db, client, web_app, user):
    sid = secrets.token_urlsafe(16)
    db.add(Session(id=sid, user_id=user.id, created_at=NOW, expires_at=NOW + timedelta(days=30)))
    await db.commit()
    client.cookies.set("sb_session", web_app.state.signer.dumps(sid))
    return sid


@pytest_asyncio.fixture
async def logged_in(db, client, web_app, make_user):
    user = await make_user(display_name="Ana")
    await login_as(db, client, web_app, user)
    return user


async def _add(db, user, server, start, end, **kw):
    b = Booking(user_id=user.id, server_id=server.id, start_at=start, end_at=end,
                resource=kw.pop("resource", "cpu"), **kw)
    db.add(b)
    await db.flush()
    return b


async def _bookings(db, server_id):
    q = (select(Booking).where(Booking.server_id == server_id).order_by(Booking.id)
         .execution_options(populate_existing=True))
    return list((await db.scalars(q)).all())


# --- auth / board ---------------------------------------------------------------------------


async def test_board_requires_login(client):
    r = await client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_board_rejects_tampered_cookie(client):
    client.cookies.set("sb_session", "not-a-signed-value")
    r = await client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_board_shows_legend_and_dots(client, logged_in, db, make_user, make_server):
    bob = await make_user(display_name="Bob")
    server = await make_server(name="srv-a")
    await _add(db, bob, server, NOW - timedelta(minutes=20), NOW + timedelta(minutes=100))
    await db.commit()
    r = await client.get("/")
    assert r.status_code == 200
    assert "dot--free_soon" in r.text and "Free soon" in r.text
    for label in ("Available", "Booked soon", "Booked", "Free soon", "Shareable", "Unavailable"):
        assert label in r.text
    for code in ("available", "booked_soon", "booked", "free_soon", "shareable", "unavailable"):
        assert f"dot--{code}" in r.text
    assert "srv-a" in r.text and "Bob" in r.text
    assert "standalone — not monitored" in r.text
    assert 'hx-get="/board/cards"' in r.text and "every 60s" in r.text


async def test_board_cards_partial_and_watch_button(client, logged_in, db, make_user, make_server):
    bob = await make_user(display_name="Bob")
    busy = await make_server(name="busy-1")
    free = await make_server(name="free-1")
    await _add(db, bob, busy, NOW - timedelta(hours=1), NOW + timedelta(hours=6))
    await db.commit()
    r = await client.get("/board/cards")
    assert r.status_code == 200 and "<html" not in r.text
    assert f'action="/watch/{busy.id}"' in r.text
    assert f'action="/watch/{free.id}"' not in r.text
    assert f"/book?server_id={free.id}" in r.text


async def test_usage_bars_for_fresh_metrics(client, logged_in, db, make_server):
    server = await make_server(name="mon-1", kind="grafana", has_gpu=True, gpu_count=1)
    from app.domain.types import GpuUsage
    await usage.save_snapshot(db, server.id, ServerUsage(NOW - timedelta(minutes=2), 37.0, 42.0,
                                                         (GpuUsage(81.0, 64.0, 0,
                                                                   "NVIDIA GeForce RTX 5090"),)))
    await db.commit()
    r = await client.get("/board/cards")
    assert "CPU" in r.text and "37%" in r.text and "42%" in r.text
    assert "RTX 5090" in r.text and "VRAM" in r.text and "81%" in r.text and "64%" in r.text
    assert "usage: unknown" not in r.text


async def test_monitoring_banner_when_all_stale(client, logged_in, db, make_server):
    server = await make_server(name="mon-1", kind="grafana")
    await usage.save_snapshot(db, server.id, ServerUsage(NOW - timedelta(hours=1), 5.0, 5.0, ()))
    await db.commit()
    r = await client.get("/")
    assert "Monitoring is down" in r.text
    assert "usage: unknown" in r.text


async def test_new_server_waits_for_first_reading(client, logged_in, db, make_server):
    await make_server(name="mon-new", kind="grafana")
    await db.commit()
    r = await client.get("/")
    assert "waiting for first reading" in r.text
    assert "Monitoring is down" not in r.text


async def test_no_monitoring_banner_when_fresh(client, logged_in, db, make_server):
    server = await make_server(name="mon-1", kind="grafana")
    await usage.save_snapshot(db, server.id, ServerUsage(NOW - timedelta(minutes=1), 5.0, 5.0, ()))
    await db.commit()
    r = await client.get("/")
    assert "Monitoring is down" not in r.text


async def test_dm_blocked_banner(client, db, web_app, make_user):
    user = await make_user(display_name="Ana", dm_blocked=True)
    await login_as(db, client, web_app, user)
    r = await client.get("/")
    assert "Unblock the bot in Telegram to receive notifications" in r.text


# --- booking --------------------------------------------------------------------------------


async def test_book_form_offers_gpu_only_for_gpu_servers(client, logged_in, db, make_server):
    cpu = await make_server(name="cpu-1")
    gpu = await make_server(name="gpu-1", has_gpu=True, gpu_count=2)
    await db.commit()
    r = await client.get(f"/book?server_id={cpu.id}")
    assert r.status_code == 200
    assert 'type="datetime-local"' in r.text and 'step="3600"' in r.text
    assert 'value="cpu_gpu"' not in r.text
    r = await client.get(f"/book?server_id={gpu.id}")
    assert 'value="cpu_gpu"' in r.text
    assert "dot--free_soon" in r.text  # legend on the booking page too


async def test_book_page_timeline_lists_bookings(client, logged_in, db, make_user, make_server):
    bob = await make_user(display_name="Bob")
    server = await make_server(name="srv")
    await _add(db, bob, server, datetime(2026, 11, 12, 8, tzinfo=UTC),
               datetime(2026, 11, 12, 10, tzinfo=UTC))
    await _add(db, bob, server, datetime(2026, 11, 30, 8, tzinfo=UTC),
               datetime(2026, 11, 30, 10, tzinfo=UTC))
    await db.commit()
    r = await client.get(f"/book?server_id={server.id}")
    assert "Thu 12 09:00" in r.text and "Bob" in r.text
    assert "Mon 30" not in r.text  # beyond 14 days


async def test_book_form_local_time_interpreted_in_rome(client, logged_in, db, make_server):
    s = await make_server(name="srv")
    await db.commit()
    r = await client.post("/book", data={"server_id": s.id, "start": "2026-11-10T14:00",
                                         "end": "2026-11-10T16:00", "resource": "cpu"})
    assert r.status_code == 303 and r.headers["location"] == "/me"
    [b] = await _bookings(db, s.id)
    assert b.start_at == datetime(2026, 11, 10, 13, tzinfo=UTC)
    assert b.end_at == datetime(2026, 11, 10, 15, tzinfo=UTC)
    assert b.user_id == logged_in.id and b.resource == "cpu"


async def test_share_warning_then_confirm(client, logged_in, db, make_user, make_server):
    bob = await make_user(display_name="Bob")
    s = await make_server(name="shr", kind="grafana")
    await _add(db, bob, s, datetime(2026, 11, 9, 10, tzinfo=UTC), datetime(2026, 11, 9, 14, tzinfo=UTC),
               shareable=True)
    await db.commit()
    form = {"server_id": s.id, "start": "2026-11-09T11:00", "end": "2026-11-09T13:00",
            "resource": "cpu"}
    r = await client.post("/book", data=form)
    assert r.status_code == 200
    assert "You'll be sharing shr with Bob from Mon 09 11:00 to Mon 09 13:00." in r.text
    assert 'name="confirm_shared" value="1"' in r.text
    assert len(await _bookings(db, s.id)) == 1

    for field in ("warned_server_id", "warned_start", "warned_end"):
        assert f'name="{field}"' in r.text
    warned = {"warned_server_id": str(s.id), "warned_start": form["start"],
              "warned_end": form["end"]}
    r = await client.post("/book", data={**form, "confirm_shared": "1", **warned})
    assert r.status_code == 303 and r.headers["location"] == "/me"
    mine = [b for b in await _bookings(db, s.id) if b.user_id == logged_in.id]
    assert len(mine) == 1 and mine[0].is_shared


async def test_conflict_error_message(client, logged_in, db, make_user, make_server):
    bob = await make_user(display_name="Bob")
    s = await make_server(name="srv")
    await _add(db, bob, s, datetime(2026, 11, 10, 13, tzinfo=UTC), datetime(2026, 11, 10, 15, tzinfo=UTC))
    await db.commit()
    r = await client.post("/book", data={"server_id": s.id, "start": "2026-11-10T15:00",
                                         "end": "2026-11-10T17:00", "resource": "cpu"})
    assert r.status_code == 400
    assert "That time overlaps another booking." in r.text
    assert len(await _bookings(db, s.id)) == 1


async def test_too_long_message_uses_max_days(client, logged_in, db, make_server):
    s = await make_server(name="srv")
    await db.commit()
    r = await client.post("/book", data={"server_id": s.id, "start": "2026-11-10T10:00",
                                         "end": "2026-11-30T10:00", "resource": "cpu"})
    assert r.status_code == 400 and "Bookings can be at most 14 days." in r.text


@pytest.mark.parametrize("form", [
    {"start": "garbage", "end": "2026-11-10T16:00", "resource": "cpu"},
    {"end": "2026-11-10T16:00", "resource": "cpu"},
    {"start": "2026-11-10T14:00", "end": "2026-11-10T16:00", "resource": "nope"},
])
async def test_invalid_form_values_400(client, logged_in, db, make_server, form):
    s = await make_server(name="srv")
    await db.commit()
    r = await client.post("/book", data={"server_id": s.id, **form})
    assert r.status_code == 400 and "Please" in r.text


async def test_invalid_server_id_400(client, logged_in):
    r = await client.post("/book", data={"server_id": "x", "start": "2026-11-10T14:00",
                                         "end": "2026-11-10T16:00", "resource": "cpu"})
    assert r.status_code == 400
    r = await client.post("/book", data={"server_id": "9999", "start": "2026-11-10T14:00",
                                         "end": "2026-11-10T16:00", "resource": "cpu"})
    assert r.status_code == 400


def test_error_text_covers_every_booking_error():
    assert set(ERROR_TEXT) == set(BookingError)
    assert ERROR_TEXT[BookingError.CONFLICT] == "That time overlaps another booking."


async def test_web_works_with_grafana_and_calendar_unreachable(
    client, logged_in, db, store, make_server
):
    await store.set(K.GRAFANA_URL, "http://grafana.invalid")
    await store.set(K.GCAL_CALENDAR_ID, "lab@group.calendar.google.com")
    s = await make_server(name="mon", kind="grafana")
    await db.commit()
    with respx.mock(assert_all_called=False) as mock:  # every outbound HTTP call would fail
        r = await client.post("/book", data={"server_id": s.id, "start": "2026-11-10T14:00",
                                             "end": "2026-11-10T16:00", "resource": "cpu"})
        assert r.status_code == 303
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/me")).status_code == 200
        assert not mock.calls
    assert len(await _bookings(db, s.id)) == 1
    kinds = (await db.scalars(select(OutboxItem.kind))).all()
    assert "calendar_create" in kinds


# --- my bookings ----------------------------------------------------------------------------


async def test_me_lists_bookings_and_subscriptions(client, logged_in, db, make_server):
    s = await make_server(name="srv-me")
    await _add(db, logged_in, s, datetime(2026, 11, 10, 8, tzinfo=UTC),
               datetime(2026, 11, 10, 10, tzinfo=UTC))
    await _add(db, logged_in, s, NOW - timedelta(minutes=20), NOW + timedelta(hours=2))
    db.add(WatchSubscription(user_id=logged_in.id, server_id=s.id, created_at=NOW))
    await db.commit()
    r = await client.get("/me")
    assert r.status_code == 200
    assert "srv-me" in r.text and "Tue 10 09:00" in r.text
    assert "Cancel" in r.text and "End now" in r.text and "Extend" in r.text
    assert "Remove" in r.text


async def test_cannot_cancel_others_booking(client, logged_in, db, make_user, make_server):
    bob = await make_user(display_name="Bob")
    s = await make_server(name="srv")
    b = await _add(db, bob, s, datetime(2026, 11, 10, 8, tzinfo=UTC),
                   datetime(2026, 11, 10, 10, tzinfo=UTC))
    await db.commit()
    r = await client.post(f"/bookings/{b.id}/cancel")
    assert r.status_code == 403
    [row] = await _bookings(db, s.id)
    assert row.status == "active"


async def test_board_offers_own_booking_actions_not_watch(client, logged_in, db, make_user,
                                                         make_server):
    bob = await make_user(display_name="Bob")
    running = await make_server(name="mine-now")
    later = await make_server(name="mine-later")
    others = await make_server(name="bobs")
    now_b = await _add(db, logged_in, running, NOW - timedelta(hours=1), NOW + timedelta(hours=3))
    later_b = await _add(db, logged_in, later, NOW + timedelta(days=1), NOW + timedelta(days=1, hours=2))
    bob_b = await _add(db, bob, others, NOW - timedelta(hours=1), NOW + timedelta(hours=3))
    await db.commit()
    r = await client.get("/board/cards")
    assert f'action="/bookings/{now_b.id}/end"' in r.text and "End booking" in r.text
    assert f'action="/watch/{running.id}"' not in r.text
    assert f'action="/bookings/{later_b.id}/cancel"' in r.text and "Cancel mine" in r.text
    assert f'action="/watch/{others.id}"' in r.text
    assert f"/bookings/{bob_b.id}/" not in r.text


async def test_board_actions_return_to_board(client, logged_in, db, make_server):
    s = await make_server(name="srv")
    b = await _add(db, logged_in, s, datetime(2026, 11, 10, 8, tzinfo=UTC),
                   datetime(2026, 11, 10, 10, tzinfo=UTC))
    await db.commit()
    r = await client.post(f"/bookings/{b.id}/cancel", data={"next": "/"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = await client.post(f"/bookings/{b.id}/cancel", data={"next": "https://evil.example"})
    assert r.headers["location"] == "/me"


async def test_cancel_own_future_booking(client, logged_in, db, make_server):
    s = await make_server(name="srv")
    b = await _add(db, logged_in, s, datetime(2026, 11, 10, 8, tzinfo=UTC),
                   datetime(2026, 11, 10, 10, tzinfo=UTC))
    await db.commit()
    r = await client.post(f"/bookings/{b.id}/cancel")
    assert r.status_code == 303 and r.headers["location"] == "/me"
    [row] = await _bookings(db, s.id)
    assert row.status == "cancelled"


async def test_cancel_missing_booking_404(client, logged_in):
    r = await client.post("/bookings/9999/cancel")
    assert r.status_code == 404


async def test_end_now_rounds_up(client, logged_in, db, make_server):
    s = await make_server(name="srv")
    b = await _add(db, logged_in, s, NOW - timedelta(minutes=20), NOW + timedelta(hours=3, minutes=40))
    await db.commit()
    r = await client.post(f"/bookings/{b.id}/end")
    assert r.status_code == 303
    [row] = await _bookings(db, s.id)
    assert row.end_at == datetime(2026, 11, 9, 11, tzinfo=UTC)


async def test_extend_own_booking(client, logged_in, db, make_server):
    s = await make_server(name="srv")
    b = await _add(db, logged_in, s, datetime(2026, 11, 10, 8, tzinfo=UTC),
                   datetime(2026, 11, 10, 10, tzinfo=UTC))
    await db.commit()
    r = await client.post(f"/bookings/{b.id}/extend", data={"end": "2026-11-10T13:00"})
    assert r.status_code == 303
    [row] = await _bookings(db, s.id)
    assert row.end_at == datetime(2026, 11, 10, 12, tzinfo=UTC)


async def test_extend_rejected_shows_message(client, logged_in, db, make_server):
    s = await make_server(name="srv")
    b = await _add(db, logged_in, s, datetime(2026, 11, 10, 8, tzinfo=UTC),
                   datetime(2026, 11, 10, 10, tzinfo=UTC))
    await db.commit()
    r = await client.post(f"/bookings/{b.id}/extend", data={"end": "2026-11-10T13:30"})
    assert r.status_code == 400 and "Bookings start and end on the hour." in r.text
    r = await client.post(f"/bookings/{b.id}/extend", data={"end": ""})
    assert r.status_code == 400


async def test_watch_subscribe_and_remove(client, logged_in, db, make_server):
    s = await make_server(name="srv")
    await db.commit()
    r = await client.post(f"/watch/{s.id}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    sub = await db.scalar(select(WatchSubscription).where(WatchSubscription.user_id == logged_in.id))
    assert sub is not None and sub.server_id == s.id
    r = await client.post(f"/watch/{sub.id}/delete")
    assert r.status_code == 303 and r.headers["location"] == "/me"
    db.expire_all()
    assert await db.scalar(select(func.count()).select_from(WatchSubscription)) == 0


async def test_watch_unknown_server_404(client, logged_in):
    assert (await client.post("/watch/9999")).status_code == 404


async def test_post_with_foreign_origin_rejected(client, logged_in, db, make_server):
    s = await make_server(name="srv")
    await db.commit()
    form = {"server_id": s.id, "start": "2026-11-10T14:00", "end": "2026-11-10T16:00",
            "resource": "cpu"}
    r = await client.post("/book", data=form, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert await _bookings(db, s.id) == []
    r = await client.post("/book", data=form, headers={"Origin": "http://test"})
    assert r.status_code == 303


# --- login / logout -------------------------------------------------------------------------


async def test_login_page_when_not_set_up(client):
    r = await client.get("/login")
    assert r.status_code == 200
    assert "The system is not set up yet" in r.text and 'href="/admin/setup"' in r.text


async def _configure_bot(db, store):
    await store.set(K.TG_BOT_USERNAME, "lab_bot")
    await store.set(K.TG_GROUP_ID, GROUP)
    await db.commit()


def _token(html: str) -> str:
    m = re.search(r"https://t\.me/lab_bot\?start=([A-Za-z0-9_\-]+)", html)
    assert m, html
    return m.group(1)


async def test_login_poll_sets_cookie_once(client, db, store):
    await _configure_bot(db, store)
    r = await client.get("/login")
    assert r.status_code == 200
    assert "Telegram will ask you to confirm — tap “Yes, log me in”." in r.text
    token = _token(r.text)
    assert f'hx-get="/login/poll/{token}"' in r.text and "every 2s" in r.text

    r = await client.get(f"/login/poll/{token}")
    assert r.status_code == 204 and "set-cookie" not in r.headers

    fake = FakeTelegram(members={42: MemberStatus.MEMBER})
    outcome = await auth.confirm_login(db, token, auth.TgUser(42, "ana", "Ana"), fake, GROUP, NOW)
    assert outcome == auth.LoginOutcome.CONFIRMED
    await db.commit()

    r = await client.get(f"/login/poll/{token}")
    assert r.status_code == 200 and r.headers["hx-redirect"] == "/"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("sb_session=")
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower() and "Path=/" in cookie
    assert "Max-Age=2592000" in cookie and "Secure" not in cookie

    r = await client.get(f"/login/poll/{token}")
    assert "set-cookie" not in r.headers and "Link expired" in r.text

    r = await client.get("/")
    assert r.status_code == 200 and "Ana" in r.text


async def test_login_poll_expired_token(client, db, store, frozen):
    await _configure_bot(db, store)
    token = _token((await client.get("/login")).text)
    frozen["now"] = NOW + timedelta(minutes=6)
    r = await client.get(f"/login/poll/{token}")
    assert r.status_code == 200 and "Link expired — reload" in r.text
    assert "hx-trigger" not in r.text
    r = await client.get("/login/poll/unknown-token")
    assert r.status_code == 200 and "Link expired" in r.text


async def test_logout_deletes_session_and_cookie(client, db, web_app, make_user):
    user = await make_user(display_name="Ana")
    sid = await login_as(db, client, web_app, user)
    r = await client.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert 'sb_session=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]
    db.expire_all()
    assert await db.get(Session, sid) is None


# --- startup --------------------------------------------------------------------------------


async def test_startup_logs_setup_code(web_app, caplog):
    with caplog.at_level(logging.WARNING, logger="app.setup"):
        async with web_app.router.lifespan_context(web_app):
            pass
    msgs = [r.getMessage() for r in caplog.records if r.name == "app.setup"]
    assert len(msgs) == 1 and re.fullmatch(r"SETUP CODE: [A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", msgs[0])
    assert logging.getLogger("httpx").level == logging.WARNING


async def test_static_assets_served(client):
    r = await client.get("/static/app.css")
    assert r.status_code == 200 and ".dot--free_soon" in r.text
    r = await client.get("/static/htmx.min.js")
    assert r.status_code == 200


# --- fix round 1 ----------------------------------------------------------------------------


async def test_htmx_unauthenticated_gets_hx_redirect(client, db):
    r = await client.get("/board/cards", headers={"HX-Request": "true"})
    assert r.status_code != 303 and "location" not in r.headers
    assert r.headers["hx-redirect"] == "/login"
    assert await db.scalar(select(func.count()).select_from(LoginToken)) == 0


@pytest.mark.parametrize("value", ["0001-01-01T00:00", "9999-12-31T23:00", "1999-12-31T10:00"])
async def test_out_of_range_dates_400(client, logged_in, db, make_server, value):
    s = await make_server(name="srv")
    b = await _add(db, logged_in, s, datetime(2026, 11, 10, 8, tzinfo=UTC),
                   datetime(2026, 11, 10, 10, tzinfo=UTC))
    await db.commit()
    r = await client.post("/book", data={"server_id": s.id, "start": value,
                                         "end": "2026-11-10T16:00", "resource": "cpu"})
    assert r.status_code == 400 and "Please" in r.text
    r = await client.post("/book", data={"server_id": s.id, "start": "2026-11-10T14:00",
                                         "end": value, "resource": "cpu"})
    assert r.status_code == 400
    r = await client.post(f"/bookings/{b.id}/extend", data={"end": value})
    assert r.status_code == 400


async def test_oversized_ids_are_not_found(client, logged_in):
    big = 99999999999
    assert (await client.post(f"/bookings/{big}/cancel")).status_code == 404
    assert (await client.post(f"/bookings/{big}/end")).status_code == 404
    r = await client.post(f"/bookings/{big}/extend", data={"end": "2026-11-10T13:00"})
    assert r.status_code == 404
    assert (await client.post(f"/watch/{big}")).status_code == 404
    assert (await client.post(f"/watch/{big}/delete")).status_code == 404
    r = await client.post("/book", data={"server_id": big, "start": "2026-11-10T14:00",
                                         "end": "2026-11-10T16:00", "resource": "cpu"})
    assert r.status_code == 400
    assert (await client.get(f"/book?server_id={big}")).status_code == 200


async def test_share_confirmation_void_when_values_changed(
    client, logged_in, db, make_user, make_server
):
    bob = await make_user(display_name="Bob")
    s = await make_server(name="shr", kind="grafana")
    await _add(db, bob, s, datetime(2026, 11, 9, 10, tzinfo=UTC), datetime(2026, 11, 9, 14, tzinfo=UTC),
               shareable=True)
    await db.commit()
    warned = {"confirm_shared": "1", "warned_server_id": str(s.id),
              "warned_start": "2026-11-09T11:00", "warned_end": "2026-11-09T13:00"}
    # The end was edited after the warning: warn again instead of booking.
    r = await client.post("/book", data={"server_id": s.id, "start": "2026-11-09T11:00",
                                         "end": "2026-11-09T14:00", "resource": "cpu", **warned})
    assert r.status_code == 200
    assert "You'll be sharing shr with Bob" in r.text
    assert 'name="warned_end" value="2026-11-09T14:00"' in r.text
    # confirm_shared alone (no warned values) is not enough either.
    r = await client.post("/book", data={"server_id": s.id, "start": "2026-11-09T11:00",
                                         "end": "2026-11-09T13:00", "resource": "cpu",
                                         "confirm_shared": "1"})
    assert r.status_code == 200 and "sharing shr with Bob" in r.text
    assert len(await _bookings(db, s.id)) == 1


def test_too_long_copy_days_or_hours():
    assert max_length_text(336) == "14 days"
    assert max_length_text(24) == "1 day"
    assert max_length_text(30) == "30 hours"
    assert max_length_text(1) == "1 hour"
    assert error_text(BookingError.TOO_LONG, Rules(max_booking_hours=30)) == (
        "Bookings can be at most 30 hours."
    )


async def test_poll_without_login_cookie_never_logs_in(client, db, store, web_app):
    await _configure_bot(db, store)
    r = await client.get("/login")
    token = _token(r.text)
    login_cookie = r.headers["set-cookie"]
    assert login_cookie.startswith("sb_login=") and "HttpOnly" in login_cookie
    assert "samesite=lax" in login_cookie.lower()
    fake = FakeTelegram(members={42: MemberStatus.MEMBER})
    await auth.confirm_login(db, token, auth.TgUser(42, "ana", "Ana"), fake, GROUP, NOW)
    await db.commit()

    transport = httpx.ASGITransport(app=web_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as attacker:
        for _ in range(2):
            r = await attacker.get(f"/login/poll/{token}")
            assert r.status_code == 204 and "set-cookie" not in r.headers
        attacker.cookies.set("sb_login", "forged")
        r = await attacker.get(f"/login/poll/{token}")
        assert r.status_code == 204 and "set-cookie" not in r.headers
    assert await db.scalar(select(func.count()).select_from(Session)) == 0

    r = await client.get(f"/login/poll/{token}")  # the browser that asked can still log in
    assert r.headers["hx-redirect"] == "/"


# --- deadlock -------------------------------------------------------------------------------


class _PgError(Exception):
    def __init__(self, sqlstate):
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


@pytest.mark.parametrize("route,target", [
    ("/book", "create_booking"),
    ("/bookings/{id}/cancel", "cancel_booking"),
    ("/bookings/{id}/end", "end_early"),
    ("/bookings/{id}/extend", "extend"),
])
async def test_deadlock_returns_busy_page(client, db, logged_in, make_server, monkeypatch,
                                          route, target):
    from sqlalchemy.exc import DBAPIError

    from app.services import booking as booking_svc

    s = await make_server(name="cpu-01")
    b = await _add(db, logged_in, s, NOW + timedelta(hours=3), NOW + timedelta(hours=5))
    await db.commit()

    async def deadlock(*a, **kw):
        raise DBAPIError("UPDATE bookings ...", {}, _PgError("40P01"))

    monkeypatch.setattr(booking_svc, target, deadlock)
    r = await client.post(route.format(id=b.id), data={
        "server_id": s.id, "start": "2026-11-10T14:00", "end": "2026-11-10T16:00",
        "resource": "cpu",
    })
    assert r.status_code == 409
    assert "The system was busy — please try again." in r.text


async def test_other_db_errors_are_not_masked(client, db, logged_in, make_server, monkeypatch):
    from sqlalchemy.exc import DBAPIError

    from app.services import booking as booking_svc

    s = await make_server(name="cpu-01")
    await db.commit()

    async def broken(*a, **kw):
        raise DBAPIError("INSERT ...", {}, _PgError("23502"))

    monkeypatch.setattr(booking_svc, "create_booking", broken)
    with pytest.raises(DBAPIError):
        await client.post("/book", data={"server_id": s.id, "start": "2026-11-10T14:00",
                                         "end": "2026-11-10T16:00", "resource": "cpu"})


async def _running(db, user, server, **kw):
    return await _add(db, user, server, NOW - timedelta(hours=1), NOW + timedelta(hours=1), **kw)


async def test_me_still_using_button_only_with_warnings(client, logged_in, db, make_server):
    s1, s2 = await make_server(name="srv-1"), await make_server(name="srv-2")
    warned = await _running(db, logged_in, s1, idle_warnings=2)
    clean = await _running(db, logged_in, s2)
    await db.commit()
    r = await client.get("/me")
    assert f"/bookings/{warned.id}/still-using" in r.text
    assert f"/bookings/{clean.id}/still-using" not in r.text
    assert "Idle warnings: 2/3" in r.text


async def test_still_using_resets_idle_state(client, logged_in, db, make_server):
    s = await make_server(name="srv")
    b = await _running(db, logged_in, s, idle_warnings=2, idle_since=NOW, idle_ram_baseline=5.0)
    await db.commit()
    r = await client.post(f"/bookings/{b.id}/still-using")
    assert r.status_code == 200 and "Idle counter reset." in r.text
    [row] = await _bookings(db, s.id)
    assert row.idle_warnings == 0 and row.idle_since is None and row.idle_ram_baseline is None


async def test_still_using_other_users_booking_forbidden(
    client, logged_in, db, make_user, make_server
):
    bob = await make_user(display_name="Bob")
    s = await make_server(name="srv")
    b = await _running(db, bob, s, idle_warnings=2)
    await db.commit()
    r = await client.post(f"/bookings/{b.id}/still-using")
    assert r.status_code == 403
    [row] = await _bookings(db, s.id)
    assert row.idle_warnings == 2


async def test_still_using_ended_booking_refused(client, logged_in, db, make_server):
    s = await make_server(name="srv")
    b = await _add(db, logged_in, s, NOW - timedelta(hours=3), NOW - timedelta(hours=1),
                   idle_warnings=2)
    await db.commit()
    r = await client.post(f"/bookings/{b.id}/still-using")
    assert "That booking isn&#39;t yours or has ended." in r.text or "isn't yours" in r.text
    [row] = await _bookings(db, s.id)
    assert row.idle_warnings == 2
    assert (await client.post("/bookings/99999/still-using")).status_code == 404


# --- per-booking sharing ----------------------------------------------------------------------


def _slots(html: str, server_name: str) -> list[list[str]]:
    """The card's Who · Uses · Until rows as lists of cell texts."""
    card = html.split(f'<h2 class="server-card__name">{server_name}</h2>')[1].split("</article>")[0]
    body = card.split('<table class="slots">')[1].split("</tbody>")[0].split("<tbody>")[1]
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)
    return [[re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
            for r in rows]


def _actions(html: str, server_name: str) -> str:
    card = html.split(f'<h2 class="server-card__name">{server_name}</h2>')[1].split("</article>")[0]
    return card.split('<div class="actions">')[1]


async def test_card_free_and_next_booking_rows(client, logged_in, db, make_user, make_server):
    bob = await make_user(display_name="Bob")
    s1 = await make_server(name="free1")
    s2 = await make_server(name="free2")
    await _add(db, bob, s2, datetime(2026, 11, 10, 8, tzinfo=UTC), datetime(2026, 11, 10, 9, tzinfo=UTC))
    await db.commit()
    r = await client.get("/")
    assert _slots(r.text, "free1") == [["Free now"], ["No upcoming bookings"]]
    assert _slots(r.text, "free2") == [["Free now"], ["Next booking", "from Tue 10 09:00"]]


async def test_card_shows_both_people_and_you(client, logged_in, db, make_user, make_server):
    bob = await make_user(display_name="Bob Longname-Sharington")
    s = await make_server(name="pair", has_gpu=True, gpu_count=1)
    await _add(db, bob, s, datetime(2026, 11, 9, 9, tzinfo=UTC), datetime(2026, 11, 9, 16, tzinfo=UTC),
               shareable=True)
    await _add(db, logged_in, s, datetime(2026, 11, 9, 10, tzinfo=UTC),
               datetime(2026, 11, 10, 12, tzinfo=UTC), is_shared=True, resource="cpu_gpu")
    await db.commit()
    r = await client.get("/")
    assert _slots(r.text, "pair") == [
        ["Bob Longname-Sharington", "CPU", "until 17:00"],
        ["You", "CPU + GPU", "until Tue 10 13:00"],
    ]
    assert 'title="Bob Longname-Sharington"' in r.text
    assert "can share" not in r.text.split('<h2 class="server-card__name">pair</h2>')[1].split("</article>")[0]
    acts = _actions(r.text, "pair")
    assert "Join" not in acts and "End booking" in acts and "Notify me" not in acts


async def test_card_join_button_for_single_shareable_booking(client, logged_in, db, make_user,
                                                             make_server):
    bob = await make_user(display_name="Bob")
    s = await make_server(name="shr")
    await _add(db, bob, s, datetime(2026, 11, 9, 9, tzinfo=UTC), datetime(2026, 11, 9, 16, tzinfo=UTC),
               shareable=True)
    mine = await make_server(name="mine")
    await _add(db, logged_in, mine, datetime(2026, 11, 9, 9, tzinfo=UTC),
               datetime(2026, 11, 9, 16, tzinfo=UTC), shareable=True)
    await db.commit()
    r = await client.get("/")
    assert _slots(r.text, "shr") == [["Bob", "CPU", "until 17:00"], ["No upcoming bookings"]]
    acts = _actions(r.text, "shr")
    assert ">Book</a>" in acts
    assert (f'href="/book?server_id={s.id}&amp;start=2026-11-09T12%3A00&amp;end=2026-11-09T17%3A00"'
            in acts)
    assert ">Join</a>" in acts
    assert "Join" not in _actions(r.text, "mine")  # never offered on your own booking
    # The link prefills the booking form; booking it asks to confirm sharing.
    page = await client.get(f"/book?server_id={s.id}&start=2026-11-09T12%3A00&end=2026-11-09T17%3A00")
    assert 'value="2026-11-09T12:00"' in page.text and 'value="2026-11-09T17:00"' in page.text


async def test_book_form_shareable_checkbox(client, logged_in, db, make_server):
    s = await make_server(name="srv")
    await db.commit()
    page = await client.get(f"/book?server_id={s.id}")
    assert "Allow sharing" in page.text
    r = await client.post("/book", data={"server_id": s.id, "start": "2026-11-10T15:00",
                                         "end": "2026-11-10T17:00", "resource": "cpu",
                                         "shareable": "on"})
    assert r.status_code == 303
    [b] = await _bookings(db, s.id)
    assert b.shareable and not b.is_shared


async def test_me_toggle_shareable_and_partner_name(client, logged_in, db, make_user, make_server):
    bob = await make_user(display_name="Bob")
    s = await make_server(name="srv")
    alone = await _add(db, logged_in, s, datetime(2026, 11, 10, 9, tzinfo=UTC),
                       datetime(2026, 11, 10, 11, tzinfo=UTC))
    shared = await _add(db, logged_in, s, datetime(2026, 11, 11, 9, tzinfo=UTC),
                        datetime(2026, 11, 11, 11, tzinfo=UTC), shareable=True)
    await _add(db, bob, s, datetime(2026, 11, 11, 10, tzinfo=UTC),
               datetime(2026, 11, 11, 12, tzinfo=UTC), is_shared=True)
    await db.commit()
    page = (await client.get("/me")).text
    assert f'action="/bookings/{alone.id}/shareable"' in page
    assert f'action="/bookings/{shared.id}/shareable"' not in page  # a partner is attached
    assert "Shared with <strong>Bob</strong>" in page and ">Shareable</span>" in page
    r = await client.post(f"/bookings/{alone.id}/shareable", data={"shareable": "1"})
    assert r.status_code == 303
    assert (await _bookings(db, s.id))[0].shareable
    r = await client.post(f"/bookings/{shared.id}/shareable", data={"shareable": "0"})
    assert r.status_code == 400 and "Sharing can" in r.text
    assert (await _bookings(db, s.id))[1].shareable


async def test_toggle_someone_elses_booking_forbidden(client, logged_in, db, make_user,
                                                      make_server):
    bob = await make_user()
    s = await make_server()
    b = await _add(db, bob, s, datetime(2026, 11, 10, 9, tzinfo=UTC),
                   datetime(2026, 11, 10, 11, tzinfo=UTC))
    await db.commit()
    r = await client.post(f"/bookings/{b.id}/shareable", data={"shareable": "1"})
    assert r.status_code == 403
    assert not (await _bookings(db, s.id))[0].shareable


async def test_about_page_explains_bot_and_rules(client, logged_in, db, store):
    await store.set(K.TG_BOT_USERNAME, "lab_booking_bot")
    await db.commit()
    r = await client.get("/about")
    assert r.status_code == 200
    for text in ("/status@lab_booking_bot", "/free@lab_booking_bot", "/notify@lab_booking_bot gpu-05",
                 "14 days", "2 h", "Mon–Fri 09:00–19:00", "Join", 'href="/about"'):
        assert text in r.text, text
