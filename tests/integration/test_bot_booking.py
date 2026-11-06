"""Telegram command menu, /help, and booking from a private chat (/book, /mybookings)."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from telegram import BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
from telegram.ext import CommandHandler

from app.bot.handlers import (
    COMMANDS, build_application, on_callback, on_command, register_commands,
)
from app.integrations.telegram_gateway import MemberStatus
from app.models import AuditLog, Booking, User
from app.services.auth import TgUser
from app.settings_store import K

G = -100123
TOPIC = 42
URL = "https://book.lab"
# Thursday 5 Nov 2026, 11:20 in Rome (UTC+1): bookings "from now" start at 11:00 local.
NOW = datetime(2026, 11, 5, 10, 20, tzinfo=timezone.utc)
HOUR = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
ANA = TgUser(id=555, username="ana", display_name="Ana Rossi")
BOB = TgUser(id=777, username="bob", display_name="Bob Bianchi")
MEMBERS_ONLY = "This bot is for members of the lab group."
CANCEL = ("✖ Cancel", "bkx")


def h(n: float) -> timedelta:
    return timedelta(hours=n)


async def _configure(db, store, bot="lab_bot", portal=URL):
    await store.set(K.TG_GROUP_ID, G)
    await store.set(K.TG_TOPIC_ID, TOPIC)
    if portal:
        await store.set(K.PORTAL_BASE_URL, portal)
    if bot:
        await store.set(K.TG_BOT_USERNAME, bot)
    await db.commit()


def _member(bot_ctx, tg=ANA):
    bot_ctx.gateway.members[tg.id] = MemberStatus.MEMBER


async def _dm(bot_ctx, command, tg=ANA, now=NOW):
    return await on_command(bot_ctx, tg, command, [], tg.id, None, True, now)


async def _group(bot_ctx, command, tg=ANA):
    return await on_command(bot_ctx, tg, command, [], G, TOPIC, False, NOW)


async def _press(bot_ctx, data, tg=ANA, now=NOW):
    return await on_callback(bot_ctx, tg, data, now)


def _data(reply, label):
    """Button data by label; "2 h" also matches "2 h (until 14:00)"."""
    return next(d for lbl, d in reply.buttons if lbl == label or lbl.startswith(label + " ("))


def _labels(reply):
    return [lbl.split(" (until")[0] for lbl, _ in reply.buttons]


async def _add(db, user, server, start, end, resource="cpu", **kw):
    b = Booking(user_id=user.id, server_id=server.id, start_at=start, end_at=end,
                resource=resource, status="active", **kw)
    db.add(b)
    await db.flush()
    return b


async def _ana(db):
    return await db.scalar(select(User).where(User.telegram_id == ANA.id))


# --- menu and /help ---------------------------------------------------------------------------


class MenuBot:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def set_my_commands(self, commands, scope=None):
        if self.fail:
            raise RuntimeError("telegram down")
        self.calls.append((type(scope), [(c.command, c.description) for c in commands]))


async def test_menu_registers_private_and_group_commands():
    bot = MenuBot()
    await register_commands(bot)
    menus = dict(bot.calls)
    private = menus[BotCommandScopeAllPrivateChats]
    assert private == [
        ("status", "All machines and who is using them"),
        ("free", "Machines free right now"),
        ("notify", "Message me when a machine is free: /notify gpu-05"),
        ("book", "Book a machine, starting now"),
        ("mybookings", "Your bookings: extend, end or cancel"),
        ("help", "What this bot can do"),
    ]
    assert [c for c, _ in menus[BotCommandScopeAllGroupChats]] == [
        "status", "free", "notify", "help"]
    assert all(len(d) <= 256 for _, d in private)


async def test_menu_matches_handled_commands(bot_ctx):
    bot = MenuBot()
    await register_commands(bot)
    menu = {c for c, _ in dict(bot.calls)[BotCommandScopeAllPrivateChats]}
    app = build_application("123456:TEST-token", bot_ctx)
    handled = set()
    for group in app.handlers.values():
        for hd in group:
            if isinstance(hd, CommandHandler):
                handled |= set(hd.commands)
    assert menu == set(COMMANDS) == handled - {"start", "bind"}


async def test_menu_failure_only_logs(caplog):
    with caplog.at_level(logging.WARNING, logger="app.bot.handlers"):
        await register_commands(MenuBot(fail=True))
        await register_commands(object())  # no set_my_commands at all
    assert "could not set the bot command menu" in caplog.text


async def test_help_private_and_group(bot_ctx, db, store):
    await _configure(db, store)
    _member(bot_ctx)
    r = await _dm(bot_ctx, "help")
    assert r.text == (
        "What I can do:\n"
        "/status — All machines and who is using them\n"
        "/free — Machines free right now\n"
        "/notify — Message me when a machine is free: /notify gpu-05\n"
        "/book — Book a machine, starting now\n"
        "/mybookings — Your bookings: extend, end or cancel\n"
        "/help — What this bot can do\n"
        f"More: {URL}/about"
    )
    r = await _group(bot_ctx, "help")
    assert "/book —" not in r.text and "/status —" in r.text
    assert "/book and /mybookings work in a private chat with @lab_bot." in r.text
    assert r.text.endswith(f"More: {URL}/about")


async def test_help_without_portal(bot_ctx, db, store):
    await _configure(db, store, portal=None)
    _member(bot_ctx)
    assert "More:" not in (await _dm(bot_ctx, "help")).text


async def test_book_and_mybookings_in_group_point_to_dm(bot_ctx, db, store):
    await _configure(db, store)
    assert (await _group(bot_ctx, "book")).text == MEMBERS_ONLY
    _member(bot_ctx)
    for cmd in ("book", "mybookings"):
        r = await _group(bot_ctx, cmd)
        assert r.text == "Message me privately to book: @lab_bot" and not r.buttons


async def test_non_member_refused(bot_ctx, db, store, make_server):
    await make_server(name="cpu-03")
    await _configure(db, store)
    assert (await _dm(bot_ctx, "book")).text == MEMBERS_ONLY
    assert (await _dm(bot_ctx, "mybookings")).text == MEMBERS_ONLY
    assert (await _press(bot_ctx, "bk")).text == MEMBERS_ONLY
    assert (await _press(bot_ctx, "mb")).text == MEMBERS_ONLY


# --- /book ------------------------------------------------------------------------------------


async def test_machine_list_shows_only_bookable_now(bot_ctx, db, store, make_server, make_user):
    bob = await make_user(display_name="Bob")
    await make_server(name="cpu-03")
    await make_server(name="gpu-05", has_gpu=True, gpu_count=1)
    busy = await make_server(name="gpu-02", has_gpu=True, gpu_count=1)
    soon = await make_server(name="gpu-03", has_gpu=True, gpu_count=2)
    share = await make_server(name="gpu-01", has_gpu=True, gpu_count=1)
    await make_server(name="old-box", enabled=False)
    await make_server(name="gone-01", archived_at=NOW, enabled=False)
    await _add(db, bob, busy, HOUR - h(1), HOUR + h(3))
    await _add(db, bob, soon, HOUR + h(2), HOUR + h(4))
    await _add(db, bob, share, HOUR - h(1), HOUR + h(4), shareable=True)
    await _configure(db, store)
    _member(bot_ctx)
    r = await _dm(bot_ctx, "book")
    assert r.text == "Which machine?"
    labels = [lbl for lbl, _ in r.buttons]
    assert labels == ["cpu-03 · CPU only", "gpu-01 · join Bob (until 15:00)",
                      "gpu-03 · 2 GPUs", "gpu-05 · 1 GPU", "✖ Cancel"]
    assert r.buttons[-1] == CANCEL


async def test_machine_list_empty(bot_ctx, db, store, make_server, make_user):
    bob = await make_user()
    s = await make_server(name="gpu-02")
    await _add(db, bob, s, HOUR, HOUR + h(5))
    await _configure(db, store)
    _member(bot_ctx)
    r = await _dm(bot_ctx, "book")
    assert r.text == "No machine is free right now. /notify <name> to get a message."
    assert not r.buttons


async def test_full_flow_books_with_gpu_and_sharing(bot_ctx, db, store, make_server):
    s = await make_server(name="gpu-05", has_gpu=True, gpu_count=1)
    await _configure(db, store)
    await store.set(K.GCAL_CALENDAR_ID, "cal@lab")
    await db.commit()
    _member(bot_ctx)
    seen = []

    r = await _dm(bot_ctx, "book")
    seen += r.buttons
    r = await _press(bot_ctx, _data(r, "gpu-05 · 1 GPU"))
    seen += r.buttons
    assert r.text == "For how long, from now?\ngpu-05, starting 11:00."
    # At 11:20, "2 h" means at least two hours: until 14:00.
    assert [lbl for lbl, _ in r.buttons] == [
        "2 h (until 14:00)", "4 h (until 16:00)", "Until 19:00 today", "Until tomorrow 19:00",
        "✖ Cancel"]
    r = await _press(bot_ctx, _data(r, "Until 19:00 today"))
    seen += r.buttons
    assert r.text == "What will you use?\ngpu-05, 11:00–19:00."
    assert [lbl for lbl, _ in r.buttons] == ["CPU", "CPU + GPU", "✖ Cancel"]
    r = await _press(bot_ctx, _data(r, "CPU + GPU"))
    seen += r.buttons
    assert r.text.startswith("Allow sharing?\ngpu-05, 11:00–19:00, CPU + GPU.")
    assert [lbl for lbl, _ in r.buttons] == ["Yes", "No", "✖ Cancel"]
    r = await _press(bot_ctx, _data(r, "Yes"))
    assert r.text == "✅ gpu-05 booked 11:00–19:00 (CPU + GPU). It's in the calendar."
    assert all(len(d.encode()) < 64 for _, d in seen)

    b = (await db.execute(select(Booking))).scalar_one()
    assert (b.server_id, b.start_at, b.end_at, b.resource, b.shareable) == (
        s.id, HOUR, HOUR + h(8), "cpu_gpu", True)
    assert b.user_id == (await _ana(db)).id
    audit = (await db.execute(select(AuditLog))).scalar_one()
    assert audit.action == "booking.create"


async def test_cpu_only_skips_resource_step_and_no_calendar_sentence(
    bot_ctx, db, store, make_server
):
    s = await make_server(name="cpu-03")
    await _configure(db, store)
    _member(bot_ctx)
    r = await _press(bot_ctx, f"bk:{s.id}")
    r = await _press(bot_ctx, _data(r, "2 h"))
    assert r.text.startswith("Allow sharing?\ncpu-03, 11:00–14:00, CPU.")
    r = await _press(bot_ctx, _data(r, "No"))
    assert r.text == "✅ cpu-03 booked 11:00–14:00 (CPU)."
    b = (await db.execute(select(Booking))).scalar_one()
    assert (b.resource, b.shareable, b.end_at) == ("cpu", False, HOUR + h(3))


async def test_durations_trimmed_by_next_booking(bot_ctx, db, store, make_server, make_user):
    bob = await make_user(display_name="Bob")
    s = await make_server(name="cpu-03")
    await _add(db, bob, s, HOUR + h(4), HOUR + h(6))
    await _configure(db, store)
    _member(bot_ctx)
    r = await _press(bot_ctx, f"bk:{s.id}")
    assert _labels(r) == ["2 h", "Until 15:00", "✖ Cancel"]
    r = await _press(bot_ctx, _data(r, "Until 15:00"))
    r = await _press(bot_ctx, _data(r, "No"))
    assert r.text == "✅ cpu-03 booked 11:00–15:00 (CPU)."


async def test_durations_trimmed_by_max_hours(bot_ctx, db, store, make_server):
    from app.domain.rules import Rules

    s = await make_server(name="cpu-03")
    await _configure(db, store)
    await store.set_rules(Rules(max_booking_hours=6))
    await db.commit()
    _member(bot_ctx)
    r = await _press(bot_ctx, f"bk:{s.id}")
    assert _labels(r) == ["2 h", "4 h", "✖ Cancel"]


async def test_join_skips_sharing_and_confirms(bot_ctx, db, store, make_server, make_user):
    bob = await make_user(display_name="Bob", telegram_id=BOB.id)
    s = await make_server(name="gpu-01", has_gpu=True, gpu_count=1)
    host = await _add(db, bob, s, HOUR - h(1), HOUR + h(6), shareable=True)
    host_id = host.id
    await _configure(db, store)
    _member(bot_ctx)
    r = await _press(bot_ctx, f"bk:{s.id}")
    # Only ends inside Bob's booking, plus "until it ends".
    assert _labels(r) == ["2 h", "4 h", "Until 17:00", "✖ Cancel"]
    r = await _press(bot_ctx, _data(r, "4 h"))
    r = await _press(bot_ctx, _data(r, "CPU"))
    assert r.text == "You'll share gpu-01 with Bob until 16:00 — confirm?"
    assert [lbl for lbl, _ in r.buttons] == ["Confirm", "✖ Cancel"]
    assert _data(r, "Confirm").endswith(f":j{host_id}")
    r = await _press(bot_ctx, _data(r, "Confirm"))
    assert r.text == "✅ gpu-01 booked 11:00–16:00 (CPU). Shared with Bob."
    db.expire_all()
    mine = (await db.execute(select(Booking).where(Booking.id != host_id))).scalar_one()
    assert mine.is_shared and not mine.shareable


async def test_own_shareable_booking_not_offered(bot_ctx, db, store, make_server, make_user):
    ana = await make_user(display_name="Ana Rossi", telegram_id=ANA.id)
    s = await make_server(name="gpu-01")
    await _add(db, ana, s, HOUR, HOUR + h(3), shareable=True)
    await _configure(db, store)
    _member(bot_ctx)
    r = await _dm(bot_ctx, "book")
    assert r.text.startswith("No machine is free right now.")


async def test_final_step_revalidates(bot_ctx, db, store, make_server, make_user):
    bob = await make_user(display_name="Bob")
    s = await make_server(name="cpu-03")
    await _configure(db, store)
    _member(bot_ctx)
    r = await _press(bot_ctx, f"bk:{s.id}")
    r = await _press(bot_ctx, _data(r, "4 h"))
    await _add(db, bob, s, HOUR, HOUR + h(2))  # taken meanwhile
    await db.commit()
    r = await _press(bot_ctx, _data(r, "No"))
    assert r.text == "Couldn't book cpu-03: That time overlaps another booking."
    assert r.buttons == [("Start again", "bk"), CANCEL]
    # An intermediate step says so too.
    r = await _press(bot_ctx, f"bk:{s.id}")
    assert r.text == "cpu-03 is not free any more."


async def test_final_step_asks_again_when_machine_became_shared(
    bot_ctx, db, store, make_server, make_user
):
    bob = await make_user(display_name="Bob")
    s = await make_server(name="cpu-03")
    await _configure(db, store)
    _member(bot_ctx)
    r = await _press(bot_ctx, f"bk:{s.id}")
    r = await _press(bot_ctx, _data(r, "2 h"))
    await _add(db, bob, s, HOUR, HOUR + h(4), shareable=True)
    await db.commit()
    r = await _press(bot_ctx, _data(r, "No"))
    assert r.text == "You'll share cpu-03 with Bob until 14:00 — confirm?"
    r = await _press(bot_ctx, _data(r, "Confirm"))
    assert r.text == "✅ cpu-03 booked 11:00–14:00 (CPU). Shared with Bob."


async def test_cancel_and_bad_data(bot_ctx, db, store, make_server):
    await _configure(db, store)
    _member(bot_ctx)
    r = await _press(bot_ctx, "bkx")
    assert r.text == "OK, nothing booked." and not r.buttons
    for bad in ("bk:x", "bk:1:zz", "bk:1:2:tpu", "bk:1:2:cpu:q", "bk:1:2:cpu:s0:extra",
                "mb:x", "mb:zz:1", "mb:x:abc"):
        assert (await _press(bot_ctx, bad)).text == "Unknown action.", bad
    assert (await _press(bot_ctx, "bk:99")).text == "That machine is not available any more."


# --- /mybookings ------------------------------------------------------------------------------


async def _mine_setup(db, store, make_user, make_server):
    ana = await make_user(display_name="Ana Rossi", telegram_id=ANA.id)
    gpu = await make_server(name="gpu-05", has_gpu=True, gpu_count=1)
    cpu = await make_server(name="cpu-03")
    running = await _add(db, ana, gpu, HOUR - h(1), HOUR + h(3), resource="cpu_gpu")
    later = await _add(db, ana, cpu, HOUR + h(23), HOUR + h(27))  # Fri 06 10:00–14:00
    await _configure(db, store)
    return ana, running, later


async def test_mybookings_lists_with_buttons(bot_ctx, db, store, make_user, make_server):
    _, running, later = await _mine_setup(db, store, make_user, make_server)
    _member(bot_ctx)
    r = await _dm(bot_ctx, "mybookings")
    assert r.text == ("Your bookings:\n"
                      "▶️ gpu-05 · now until 14:00 · CPU + GPU\n"
                      "🗓 cpu-03 · Fri 06 10:00–14:00 · CPU")
    assert r.buttons == [("gpu-05: Extend +2 h", f"mb:x:{running.id}"),
                         ("gpu-05: End now", f"mb:e:{running.id}"),
                         ("cpu-03: Cancel", f"mb:c:{later.id}")]


async def test_mybookings_empty(bot_ctx, db, store):
    await _configure(db, store)
    _member(bot_ctx)
    r = await _dm(bot_ctx, "mybookings")
    assert r.text == "You have no current or upcoming bookings. Book one with /book."


async def test_mybookings_shows_partner(bot_ctx, db, store, make_user, make_server):
    ana = await make_user(display_name="Ana Rossi", telegram_id=ANA.id)
    bob = await make_user(display_name="Bob")
    s = await make_server(name="gpu-01")
    await _add(db, ana, s, HOUR - h(1), HOUR + h(4), shareable=True)
    await _add(db, bob, s, HOUR, HOUR + h(2), is_shared=True)
    await _configure(db, store)
    _member(bot_ctx)
    r = await _dm(bot_ctx, "mybookings")
    assert "▶️ gpu-01 · now until 15:00 · CPU\n    shared with Bob" in r.text


async def test_mybookings_extend_end_cancel(bot_ctx, db, store, make_user, make_server):
    _, running, later = await _mine_setup(db, store, make_user, make_server)
    rid, lid = running.id, later.id
    _member(bot_ctx)
    r = await _press(bot_ctx, f"mb:x:{rid}")
    assert r.text.startswith("✅ gpu-05 extended to 16:00.\n\nYour bookings:")
    assert "gpu-05 · now until 16:00" in r.text
    db.expire_all()
    assert (await db.get(Booking, rid)).end_at == HOUR + h(5)

    r = await _press(bot_ctx, f"mb:e:{rid}")
    assert r.text.startswith("✅ gpu-05 booking ended. The machine is free now.")
    assert "gpu-05" not in r.text.split("\n\n", 1)[1]
    db.expire_all()
    assert (await db.get(Booking, rid)).status == "completed"

    r = await _press(bot_ctx, f"mb:c:{lid}")
    assert r.text == "Cancel cpu-03 Fri 06 10:00–14:00?"
    assert r.buttons == [("Yes, cancel", f"mb:cy:{lid}"), ("No", "mb")]
    r = await _press(bot_ctx, "mb")
    assert "cpu-03" in r.text  # "No" leaves it alone
    r = await _press(bot_ctx, f"mb:cy:{lid}")
    assert r.text.startswith("✅ cpu-03 booking cancelled.")
    assert "cpu-03" not in r.text.split("\n\n", 1)[1]
    db.expire_all()
    assert (await db.get(Booking, lid)).status == "cancelled"
    actions = {a.action for a in (await db.execute(select(AuditLog))).scalars()}
    assert {"booking.extend", "booking.end_early", "booking.cancel"} <= actions


async def test_mybookings_extend_error_uses_shared_text(bot_ctx, db, store, make_user,
                                                        make_server):
    ana = await make_user(display_name="Ana Rossi", telegram_id=ANA.id)
    bob = await make_user(display_name="Bob")
    s = await make_server(name="gpu-01")
    await _add(db, bob, s, HOUR - h(1), HOUR + h(3), shareable=True)
    mine = await _add(db, ana, s, HOUR, HOUR + h(2), is_shared=True)
    mid = mine.id
    await _configure(db, store)
    _member(bot_ctx)
    r = await _press(bot_ctx, f"mb:x:{mid}")
    assert r.text.startswith(
        "Couldn't extend gpu-01: You can only join until Bob's booking ends at 14:00.")
    db.expire_all()
    assert (await db.get(Booking, mid)).end_at == HOUR + h(2)


async def test_mybookings_someone_elses_booking_refused(bot_ctx, db, store, make_user,
                                                        make_server):
    _, running, later = await _mine_setup(db, store, make_user, make_server)
    rid, lid, end = running.id, later.id, running.end_at
    _member(bot_ctx, BOB)
    for data in (f"mb:x:{rid}", f"mb:e:{rid}", f"mb:c:{rid}", f"mb:cy:{lid}"):
        r = await _press(bot_ctx, data, tg=BOB)
        assert r.text == "That booking isn't yours or has ended.", data
    db.expire_all()
    b = await db.get(Booking, rid)
    assert b.end_at == end and b.status == "active"
    assert (await db.get(Booking, lid)).status == "active"
