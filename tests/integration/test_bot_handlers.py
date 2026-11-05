from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler

from app.bot.handlers import (
    Reply, build_application, on_bind, on_callback, on_command, on_group_message, on_start,
)
from app.integrations.telegram_gateway import MemberStatus
from app.models import Booking, LoginToken, OutboxItem, Setting, TelegramTopic, User, WatchSubscription
from app.services import watchlist
from app.services.auth import TgUser, create_login_token
from app.domain.types import ServerStatus, StatusCode
from app.settings_store import K, SettingsStore

G = -100123
TOPIC = 42
URL = "https://book.lab"
NOW = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
ANA = TgUser(id=555, username="ana", display_name="Ana Rossi")
MEMBERS_ONLY = "This bot is for members of the lab group."


async def _configure(db, store, group=G, topic=TOPIC, portal=URL):
    if group is not None:
        await store.set(K.TG_GROUP_ID, group)
    if topic is not None:
        await store.set(K.TG_TOPIC_ID, topic)
    if portal is not None:
        await store.set(K.PORTAL_BASE_URL, portal)
    await db.commit()


def _member(bot_ctx, tg=ANA, status=MemberStatus.MEMBER):
    bot_ctx.gateway.members[tg.id] = status


async def _cmd(bot_ctx, command, args=(), chat_id=G, thread_id=TOPIC, is_private=False, tg=ANA):
    return await on_command(
        bot_ctx, tg, command, list(args), chat_id, thread_id, is_private, NOW
    )


async def test_command_in_other_topic_ignored(bot_ctx, db, store):
    await _configure(db, store)
    _member(bot_ctx)
    assert await _cmd(bot_ctx, "status", thread_id=999) is None
    assert await _cmd(bot_ctx, "status", thread_id=None) is None
    assert await _cmd(bot_ctx, "status", chat_id=-100999) is None


async def test_command_in_booking_topic_answers(bot_ctx, db, store, make_server):
    await make_server(name="gpu-01")
    await make_server(name="old-box", enabled=False)
    await _configure(db, store)
    _member(bot_ctx)
    r = await _cmd(bot_ctx, "status")
    assert isinstance(r, Reply)
    assert "🟢 gpu-01 — Available" in r.text
    assert "old-box" not in r.text
    assert r.text.endswith(f"Book: {URL}")
    free = await _cmd(bot_ctx, "free")
    assert "gpu-01" in free.text


async def test_status_server_detail_case_insensitive(bot_ctx, db, store, make_server):
    await make_server(name="GPU-01")
    await make_server(name="old-box", enabled=False)
    await _configure(db, store)
    _member(bot_ctx)
    r = await _cmd(bot_ctx, "status", ["gpu-01"])
    assert "GPU-01" in r.text and "standalone — not monitored" in r.text
    r = await _cmd(bot_ctx, "status", ["old-box"])
    assert r.text == 'Unknown server "old-box". Try /status.'


async def test_command_in_dm_answers_members_only(bot_ctx, db, store, make_server):
    await make_server(name="gpu-01")
    await _configure(db, store)
    r = await _cmd(bot_ctx, "status", chat_id=ANA.id, thread_id=None, is_private=True)
    assert r.text == MEMBERS_ONLY
    _member(bot_ctx)
    r = await _cmd(bot_ctx, "status", chat_id=ANA.id, thread_id=None, is_private=True)
    assert "gpu-01" in r.text


async def test_command_before_setup(bot_ctx):
    r = await _cmd(bot_ctx, "status", chat_id=ANA.id, thread_id=None, is_private=True)
    assert r.text == "The booking system is not set up yet."


async def _open_window(db, store, until=NOW + timedelta(minutes=10)):
    await store.set(K.SETUP_BIND_OPEN_UNTIL, until.isoformat())
    await db.commit()


async def _bind(bot_ctx, chat_id=G, thread_id=TOPIC, title="Machines", chat_title="Lab", tg=ANA):
    return await on_bind(bot_ctx, tg, chat_id, thread_id, title, chat_title, NOW)


async def test_bind_requires_admin(bot_ctx, db, store):
    await _open_window(db, store)
    r = await _bind(bot_ctx)
    assert r.text == "Only group admins can do that."
    bot_ctx.gateway.admins.add(ANA.id)
    r = await _bind(bot_ctx, thread_id=None, title=None)
    assert r.text == "Send /bind inside the machine-booking topic."
    assert await db.get(Setting, K.TG_GROUP_ID) is None


async def test_bind_sets_group_and_topic(bot_ctx, db, store, box):
    await _open_window(db, store)
    bot_ctx.gateway.admins.add(ANA.id)
    r = await _bind(bot_ctx)
    assert r.text == "✅ This topic is now the machine-booking topic."
    db.expire_all()
    fresh = SettingsStore(db, box)
    assert await fresh.get(K.TG_GROUP_ID) == G
    assert await fresh.get(K.TG_TOPIC_ID) == TOPIC
    assert await fresh.get(K.TG_GROUP_TITLE) == "Lab"
    assert await fresh.get(K.SETUP_BIND_OPEN_UNTIL) is None  # window cleared
    topic = await db.get(TelegramTopic, TOPIC)
    assert topic.title == "Machines" and topic.last_seen_at == NOW


async def test_bind_unbound_without_window_refused(bot_ctx, db):
    bot_ctx.gateway.admins.add(ANA.id)
    r = await _bind(bot_ctx)
    assert r.text == "Binding is only possible while the setup wizard is waiting for it."
    assert await db.get(Setting, K.TG_GROUP_ID) is None


async def test_bind_expired_window_refused(bot_ctx, db, store):
    await _open_window(db, store, until=NOW)  # now == until: closed
    bot_ctx.gateway.admins.add(ANA.id)
    r = await _bind(bot_ctx)
    assert r.text == "Binding is only possible while the setup wizard is waiting for it."
    assert await db.get(Setting, K.TG_GROUP_ID) is None


async def test_bind_other_group_cannot_rebind(bot_ctx, db, store, box):
    await _configure(db, store)
    other = -100999
    bot_ctx.gateway.admins_by_chat = {other: {ANA.id}, G: set()}
    r = await _bind(bot_ctx, chat_id=other, thread_id=5)
    assert r.text == "This bot is already bound to another group."
    db.expire_all()
    fresh = SettingsStore(db, box)
    assert await fresh.get(K.TG_GROUP_ID) == G and await fresh.get(K.TG_TOPIC_ID) == TOPIC
    assert await db.get(TelegramTopic, 5) is None


async def test_bind_same_group_admin_reselects_topic(bot_ctx, db, store, box):
    await _configure(db, store)  # bound, no window open
    bot_ctx.gateway.admins_by_chat = {G: {ANA.id}}
    r = await _bind(bot_ctx, thread_id=77, title="New topic")
    assert r.text == "✅ This topic is now the machine-booking topic."
    db.expire_all()
    assert await SettingsStore(db, box).get(K.TG_TOPIC_ID) == 77


async def test_bind_without_title_uses_placeholder(bot_ctx, db, store):
    await _open_window(db, store)
    bot_ctx.gateway.admins.add(ANA.id)
    await _bind(bot_ctx, title=None)
    assert (await db.get(TelegramTopic, TOPIC)).title == f"Topic #{TOPIC}"


async def test_topic_discovery_records_title(bot_ctx, db, store):
    await on_group_message(bot_ctx, G, 7, "Ignored", NOW)  # group not configured yet
    assert (await db.execute(select(TelegramTopic))).first() is None
    await _configure(db, store)
    await on_group_message(bot_ctx, -100999, 7, "Other group", NOW)
    await on_group_message(bot_ctx, G, 7, "GPU chat", NOW)
    later = NOW + timedelta(hours=1)
    await on_group_message(bot_ctx, G, 7, None, later)  # keeps title, bumps last_seen
    await on_group_message(bot_ctx, G, 8, None, NOW)
    db.expire_all()
    rows = {t.thread_id: t for t in (await db.execute(select(TelegramTopic))).scalars()}
    assert set(rows) == {7, 8}
    assert rows[7].title == "GPU chat" and rows[7].last_seen_at == later
    assert rows[8].title == "Topic #8"


async def test_start_without_payload_greets(bot_ctx, db, store):
    r = await on_start(bot_ctx, ANA, None, NOW)
    assert r.text == "👋 Hi! Use /status or /free here."
    await _configure(db, store)
    r = await on_start(bot_ctx, ANA, None, NOW)
    assert r.text == f"👋 Hi! Use /status or /free here, or book on the portal: {URL}"
    assert r.buttons is None


async def test_start_with_token_asks_then_button_confirms_login(bot_ctx, db, store):
    await _configure(db, store)
    _member(bot_ctx)
    token = await create_login_token(db, NOW)
    await db.commit()
    r = await on_start(bot_ctx, ANA, token, NOW)
    assert r.text == (
        "🔐 Log in to the lab booking portal?\n\nOnly tap the button if you just clicked "
        '"Log in" in your own browser. Never tap it for a link someone sent you.'
    )
    assert r.buttons == [("✅ Yes, log me in", f"login:{token}")]
    db.expire_all()
    assert (await db.get(LoginToken, token)).confirmed_at is None  # /start alone never confirms

    r = await on_callback(bot_ctx, ANA, f"login:{token}", NOW)
    assert r.text == "✅ You're logged in — return to your browser."
    db.expire_all()
    assert (await db.get(LoginToken, token)).confirmed_at == NOW
    user = (await db.execute(select(User).where(User.telegram_id == ANA.id))).scalar_one()
    assert user.display_name == "Ana Rossi"


async def test_login_callback_failures(bot_ctx, db, store):
    r = await on_start(bot_ctx, ANA, "tok", NOW)
    assert r.text == "The booking system is not set up yet." and r.buttons is None
    assert (await on_callback(bot_ctx, ANA, "login:tok", NOW)).text == (
        "The booking system is not set up yet."
    )
    await _configure(db, store)
    expired = "⌛ This login link has expired. Go back to the portal and click Log in again."
    assert (await on_callback(bot_ctx, ANA, "login:nope", NOW)).text == expired
    token = await create_login_token(db, NOW)
    await db.commit()
    assert (await on_callback(bot_ctx, ANA, f"login:{token}", NOW)).text == MEMBERS_ONLY
    _member(bot_ctx)
    late = NOW + timedelta(minutes=6)
    assert (await on_callback(bot_ctx, ANA, f"login:{token}", late)).text == expired


@pytest.fixture
async def booking_with_warnings(db, booking):
    booking.idle_warnings = 2
    booking.idle_since = NOW
    user = await db.get(User, booking.user_id)
    user.telegram_id = ANA.id
    await db.commit()
    return booking


async def test_still_using_callback_resets(bot_ctx, db, store, booking_with_warnings):
    await _configure(db, store)
    _member(bot_ctx)
    b = booking_with_warnings
    r = await on_callback(bot_ctx, ANA, f"still:{b.id}", b.start_at + timedelta(hours=1))
    assert r.text == "👍 Thanks — idle counter reset."
    bid = b.id
    db.expire_all()
    fresh = await db.get(Booking, bid)
    assert fresh.idle_warnings == 0 and fresh.idle_since is None


async def test_still_using_other_user_refused(bot_ctx, db, store, booking):
    booking.idle_warnings = 2
    await _configure(db, store)
    bob = TgUser(id=777, username="bob", display_name="Bob")
    _member(bot_ctx, bob)
    bid, start = booking.id, booking.start_at
    r = await on_callback(bot_ctx, bob, f"still:{bid}", start)
    assert r.text == "That booking isn't yours or has ended."
    db.expire_all()
    assert (await db.get(Booking, bid)).idle_warnings == 2
    # non-members are refused before anything else
    r = await on_callback(bot_ctx, ANA, f"still:{bid}", start)
    assert r.text == MEMBERS_ONLY


async def test_notify_command_subscribes_once(bot_ctx, db, store, make_server):
    server = await make_server(name="gpu-01")
    await _configure(db, store)
    _member(bot_ctx)
    r = await _cmd(bot_ctx, "notify", ["GPU-01"])
    assert r.text == "🔔 I'll message you when gpu-01 is free."
    r = await _cmd(bot_ctx, "notify", ["gpu-01"])
    assert r.text == "You're already on the list for gpu-01."
    assert (await _cmd(bot_ctx, "notify")).text == "Usage: /notify <server>"
    assert (await _cmd(bot_ctx, "notify", ["nope"])).text == 'Unknown server "nope". Try /status.'
    subs = (await db.execute(select(WatchSubscription))).scalars().all()
    assert len(subs) == 1 and subs[0].server_id == server.id


async def test_watchlist_fire_dms_and_clears(db, store, make_user, make_server):
    await store.set(K.PORTAL_BASE_URL, URL)
    server = await make_server(name="gpu-01")
    other = await make_server(name="gpu-02")
    u1, u2 = await make_user(), await make_user()
    assert await watchlist.subscribe(db, u1.id, server.id, NOW) is True
    assert await watchlist.subscribe(db, u1.id, server.id, NOW) is False
    assert await watchlist.subscribe(db, u2.id, server.id, NOW) is True
    assert await watchlist.subscribe(db, u2.id, other.id, NOW) is True
    status = ServerStatus(StatusCode.AVAILABLE, "Available", None, None, ())
    assert await watchlist.fire(db, store, server, status, NOW) == 2
    items = (await db.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars().all()
    assert [i.payload["chat_id"] for i in items] == [u1.telegram_id, u2.telegram_id]
    assert items[0].payload["text"] == (
        f"🟢 gpu-01 is now Available. Book: {URL}/book?server_id={server.id}"
    )
    left = await watchlist.for_user(db, u2.id)
    assert [s.server_id for s in left] == [other.id]
    assert await watchlist.fire(db, store, server, status, NOW) == 0


async def test_watchlist_fire_without_portal_and_unsubscribe(db, store, make_user, make_server):
    server = await make_server(name="gpu-01")
    u1, u2 = await make_user(), await make_user()
    await watchlist.subscribe(db, u1.id, server.id, NOW)
    [sub] = await watchlist.for_user(db, u1.id)
    await watchlist.unsubscribe(db, sub.id, u2.id)  # not theirs: no-op
    assert len(await watchlist.for_user(db, u1.id)) == 1
    await watchlist.subscribe(db, u2.id, server.id, NOW)
    await watchlist.unsubscribe(db, sub.id, u1.id)
    assert await watchlist.for_user(db, u1.id) == []
    status = ServerStatus(StatusCode.AVAILABLE, "Available", None, None, ())
    assert await watchlist.fire(db, store, server, status, NOW) == 1
    item = (await db.execute(select(OutboxItem))).scalar_one()
    assert item.payload["text"] == "🟢 gpu-01 is now Available."


def test_build_application_registers_handlers(bot_ctx):
    app = build_application("123456:TEST-token", bot_ctx)
    assert isinstance(app, Application)
    handlers = [h for group in app.handlers.values() for h in group]
    commands = set()
    for h in handlers:
        if isinstance(h, CommandHandler):
            commands |= set(h.commands)
    assert commands == {"start", "bind", "status", "free", "notify"}
    assert any(isinstance(h, CallbackQueryHandler) for h in handlers)
    assert any(isinstance(h, MessageHandler) for h in handlers)
    assert app.error_handlers
