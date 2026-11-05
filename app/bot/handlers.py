"""Telegram bot handlers.

The `on_*` functions are PTB-independent so they can be tested directly; each opens
its own session, commits on success and rolls back on error. `build_application`
adapts PTB updates to them.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
from datetime import datetime, timezone

import telegram
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import clock
from app.bot import format as fmt
from app.crypto import SecretBox
from app.integrations.telegram_gateway import MemberStatus, TelegramGateway
from app.models import Server, TelegramTopic, User
from app.services import auth, board, booking, watchlist
from app.services.auth import LoginOutcome, TgUser
from app.services.inventory import list_servers
from app.settings_store import K, SettingsStore

log = logging.getLogger(__name__)

NOT_SET_UP = "The booking system is not set up yet."
MEMBERS_ONLY = "This bot is for members of the lab group."
ADMINS_ONLY = "Only group admins can do that."
BIND_IN_TOPIC = "Send /bind inside the machine-booking topic."
BOUND = "✅ This topic is now the machine-booking topic."
BOUND_ELSEWHERE = "This bot is already bound to another group."
BIND_CLOSED = "Binding is only possible while the setup wizard is waiting for it."
LOGIN_PROMPT = (
    "🔐 Log in to the lab booking portal?\n\n"
    'Only tap the button if you just clicked "Log in" in your own browser. '
    "Never tap it for a link someone sent you."
)
LOGIN_BUTTON = "✅ Yes, log me in"
LOGGED_IN = "✅ You're logged in — return to your browser."
LOGIN_EXPIRED = "⌛ This login link has expired. Go back to the portal and click Log in again."
STILL_USING_OK = "👍 Thanks — idle counter reset."
STILL_USING_REFUSED = "That booking isn't yours or has ended."
NOTIFY_USAGE = "Usage: /notify <server>"
UNKNOWN_ACTION = "Unknown action."

COMMANDS = ("status", "free", "notify")


@dataclass(frozen=True)
class BotContext:
    sessionmaker: async_sessionmaker[AsyncSession]
    gateway: TelegramGateway
    box: SecretBox


@dataclass(frozen=True)
class Reply:
    text: str | None  # None: nothing to say
    buttons: list[tuple[str, str]] | None = None
    ephemeral: bool = False  # group chatter that should clean itself up


@asynccontextmanager
async def _session(ctx: BotContext) -> AsyncIterator[tuple[AsyncSession, SettingsStore]]:
    async with ctx.sessionmaker() as db:
        try:
            yield db, SettingsStore(db, ctx.box)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def _portal(store: SettingsStore) -> str:
    return (await store.get(K.PORTAL_BASE_URL)) or ""


async def _upsert_topic(
    db: AsyncSession, thread_id: int, title: str | None, now: datetime
) -> None:
    topic = await db.get(TelegramTopic, thread_id)
    if topic is None:
        db.add(TelegramTopic(
            thread_id=thread_id, title=title or f"Topic #{thread_id}", last_seen_at=now
        ))
    else:
        if title:
            topic.title = title
        topic.last_seen_at = now
    await db.flush()


async def _find_server(db: AsyncSession, name: str) -> Server | None:
    wanted = name.casefold()
    return next(
        (s for s in await list_servers(db, include_disabled=False) if s.name.casefold() == wanted),
        None,
    )


async def _get_or_create_user(db: AsyncSession, tg: TgUser, status: MemberStatus) -> User:
    user = await db.scalar(select(User).where(User.telegram_id == tg.id))
    if user is None:
        user = User(
            telegram_id=tg.id, display_name=tg.display_name, username=tg.username,
            is_admin=status == MemberStatus.ADMIN,
        )
        db.add(user)
        await db.flush()
    return user


async def _clear_dm_blocked(db: AsyncSession, telegram_id: int) -> None:
    """The user just messaged the bot privately, so they have unblocked it."""
    await db.execute(
        update(User).where(User.telegram_id == telegram_id, User.dm_blocked.is_(True))
        .values(dm_blocked=False)
    )


async def on_start(ctx: BotContext, tg: TgUser, payload: str | None, now: datetime) -> Reply:
    async with _session(ctx) as (db, store):
        await _clear_dm_blocked(db, tg.id)
        if payload:
            if await store.get(K.TG_GROUP_ID) is None:
                return Reply(NOT_SET_UP)
            return Reply(LOGIN_PROMPT, [(LOGIN_BUTTON, f"login:{payload}")])
        portal = await _portal(store)
        bot = await store.get(K.TG_BOT_USERNAME)
    text = "👋 Hi! Use /status or /free here."
    if bot:
        # With privacy mode on, Telegram reliably delivers only commands addressed to the bot.
        text += f" In the group, use /status@{bot} or /free@{bot}."
    if portal:
        text += f" Book on the portal: {portal}"
    return Reply(text)


def _window_open(value: str | None, now: datetime) -> bool:
    if not value:
        return False
    try:
        until = datetime.fromisoformat(value)
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return now < until


async def on_bind(
    ctx: BotContext, tg: TgUser, chat_id: int, thread_id: int | None, title: str | None,
    chat_title: str | None, now: datetime,
) -> Reply:
    if tg.id not in await ctx.gateway.get_admin_ids(chat_id):
        return Reply(ADMINS_ONLY, ephemeral=True)
    if thread_id is None:
        return Reply(BIND_IN_TOPIC, ephemeral=True)
    async with _session(ctx) as (db, store):
        group = await store.get(K.TG_GROUP_ID)
        if group is not None and group != chat_id:
            return Reply(BOUND_ELSEWHERE, ephemeral=True)
        if group is None:
            # First binding: only while the setup wizard has opened a window.
            if not _window_open(await store.get(K.SETUP_BIND_OPEN_UNTIL), now):
                return Reply(BIND_CLOSED, ephemeral=True)
            await store.set(K.TG_GROUP_ID, chat_id)
            if chat_title:
                await store.set(K.TG_GROUP_TITLE, chat_title)
            await store.delete(K.SETUP_BIND_OPEN_UNTIL)
        unchanged = group == chat_id and await store.get(K.TG_TOPIC_ID) == thread_id
        if not unchanged:
            await store.set(K.TG_TOPIC_ID, thread_id)
        await _upsert_topic(db, thread_id, title, now)
    if unchanged:
        return Reply(None, ephemeral=True)  # already bound here: stay quiet
    return Reply(BOUND, ephemeral=True)


async def on_group_message(
    ctx: BotContext, chat_id: int, thread_id: int | None, topic_title: str | None, now: datetime
) -> None:
    if thread_id is None:
        return
    async with _session(ctx) as (db, store):
        group = await store.get(K.TG_GROUP_ID)
        if group is None or chat_id != group:
            return
        await _upsert_topic(db, thread_id, topic_title, now)


async def on_command(
    ctx: BotContext, tg: TgUser, command: str, args: list[str], chat_id: int,
    thread_id: int | None, is_private: bool, now: datetime,
) -> Reply | None:
    async with _session(ctx) as (db, store):
        if is_private:
            await _clear_dm_blocked(db, tg.id)
        group = await store.get(K.TG_GROUP_ID)
        if not is_private:
            topic = await store.get(K.TG_TOPIC_ID)
            if group is None or chat_id != group or thread_id != topic:
                return None
        if command not in COMMANDS:
            return None
        if group is None:
            return Reply(NOT_SET_UP)
        status = await ctx.gateway.get_member_status(group, tg.id)
        if status == MemberStatus.NOT_MEMBER:
            return Reply(MEMBERS_ONLY)

        portal = await _portal(store)
        tz = (await store.get_rules()).timezone
        arg = " ".join(args).strip()

        if command in ("status", "free") and not (command == "status" and arg):
            cards = [c for c in await board.build_board(db, store, now) if c.server.enabled]
            if command == "free":
                return Reply(fmt.free_list(cards, tz, portal))
            return Reply(fmt.status_list(cards, tz, portal))

        if command == "notify" and not arg:
            return Reply(NOTIFY_USAGE)
        server = await _find_server(db, arg)
        if server is None:
            return Reply(f'Unknown server "{arg}". Try /status.')
        if command == "status":
            card = await board.build_card(db, store, server.id, now)
            return Reply(fmt.server_detail(card, tz, portal, now))
        user = await _get_or_create_user(db, tg, status)
        if await watchlist.subscribe(db, user.id, server.id, now):
            return Reply(f"🔔 I'll message you when {server.name} is free.")
        return Reply(f"You're already on the list for {server.name}.")


async def _login(ctx: BotContext, tg: TgUser, token: str, now: datetime) -> Reply:
    async with _session(ctx) as (db, store):
        group = await store.get(K.TG_GROUP_ID)
        if group is None:
            return Reply(NOT_SET_UP)
        outcome = await auth.confirm_login(db, token, tg, ctx.gateway, group, now)
    if outcome == LoginOutcome.CONFIRMED:
        return Reply(LOGGED_IN)
    if outcome == LoginOutcome.NOT_MEMBER:
        return Reply(MEMBERS_ONLY)
    return Reply(LOGIN_EXPIRED)


async def _still_using(ctx: BotContext, tg: TgUser, raw_id: str, now: datetime) -> Reply:
    try:
        booking_id = int(raw_id)
    except ValueError:
        return Reply(UNKNOWN_ACTION)
    async with _session(ctx) as (db, store):
        group = await store.get(K.TG_GROUP_ID)
        if group is None:
            return Reply(NOT_SET_UP)
        if await ctx.gateway.get_member_status(group, tg.id) == MemberStatus.NOT_MEMBER:
            return Reply(MEMBERS_ONLY)
        if await booking.acknowledge_still_using(db, booking_id, tg.id, now):
            return Reply(STILL_USING_OK)
    return Reply(STILL_USING_REFUSED)


async def on_callback(ctx: BotContext, tg: TgUser, data: str, now: datetime) -> Reply:
    action, _, value = data.partition(":")
    if action == "login" and value:
        return await _login(ctx, tg, value, now)
    if action == "still" and value:
        return await _still_using(ctx, tg, value, now)
    return Reply(UNKNOWN_ACTION)


# --- PTB wiring -------------------------------------------------------------------------


def _tg_user(user: telegram.User) -> TgUser:
    return TgUser(id=user.id, username=user.username, display_name=user.full_name)


def _thread_id(msg: telegram.Message) -> int | None:
    return msg.message_thread_id if msg.is_topic_message else None


def _topic_title(msg: telegram.Message) -> str | None:
    if msg.forum_topic_created:
        return msg.forum_topic_created.name
    if msg.forum_topic_edited and msg.forum_topic_edited.name:
        return msg.forum_topic_edited.name
    parent = msg.reply_to_message
    if parent is not None and parent.forum_topic_created:
        return parent.forum_topic_created.name
    return None


def _markup(buttons: list[tuple[str, str]] | None) -> telegram.InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return telegram.InlineKeyboardMarkup(
        [[telegram.InlineKeyboardButton(label, callback_data=data)] for label, data in buttons]
    )


async def _reply(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE, reply: Reply):
    msg = update.effective_message
    await context.bot.send_message(
        msg.chat_id, reply.text, message_thread_id=_thread_id(msg),
        reply_markup=_markup(reply.buttons),
    )


EPHEMERAL_TTL = 30  # seconds before bind chatter is deleted


async def delete_messages(bot, chat_id: int, message_ids: list[int]) -> None:
    """Best-effort delete; a missing permission or an already-gone message is not an error."""
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id, mid)
        except TelegramError as e:
            log.info("could not delete message %s in %s: %s", mid, chat_id, e)


def schedule_deletion(context: ContextTypes.DEFAULT_TYPE, chat_id: int, ids: list[int]) -> None:
    async def job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await delete_messages(ctx.bot, chat_id, ids)

    if context.job_queue is not None:
        context.job_queue.run_once(job, EPHEMERAL_TTL)
        return

    async def later() -> None:
        await asyncio.sleep(EPHEMERAL_TTL)
        await delete_messages(context.bot, chat_id, ids)

    task = asyncio.get_running_loop().create_task(later())
    _background.add(task)
    task.add_done_callback(_background.discard)


_background: set[asyncio.Task] = set()


def build_application(token: str, ctx: BotContext) -> Application:
    app = Application.builder().token(token).build()
    new_messages = filters.UpdateType.MESSAGE

    async def start(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        payload = context.args[0] if context.args else None
        reply = await on_start(ctx, _tg_user(update.effective_user), payload, clock.utcnow())
        await _reply(update, context, reply)

    async def bind(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        reply = await on_bind(
            ctx, _tg_user(update.effective_user), msg.chat_id, _thread_id(msg),
            _topic_title(msg), msg.chat.title, clock.utcnow(),
        )
        if not reply.text:
            return
        sent = await context.bot.send_message(
            msg.chat_id, reply.text, message_thread_id=_thread_id(msg),
            reply_markup=_markup(reply.buttons),
        )
        if reply.ephemeral:
            schedule_deletion(context, msg.chat_id, [sent.message_id, msg.message_id])

    async def command(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        name = msg.text.split()[0].lstrip("/").split("@")[0].lower()
        reply = await on_command(
            ctx, _tg_user(update.effective_user), name, list(context.args or []), msg.chat_id,
            _thread_id(msg), update.effective_chat.type == telegram.constants.ChatType.PRIVATE,
            clock.utcnow(),
        )
        if reply is not None:
            await _reply(update, context, reply)

    async def callback(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        try:
            reply = await on_callback(
                ctx, _tg_user(query.from_user), query.data or "", clock.utcnow()
            )
        finally:
            await query.answer()  # never leave the client spinner hanging
        try:
            await query.edit_message_text(reply.text, reply_markup=_markup(reply.buttons))
        except BadRequest:
            log.warning("could not edit callback message", exc_info=True)

    async def group_message(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        await on_group_message(ctx, msg.chat_id, _thread_id(msg), _topic_title(msg), clock.utcnow())

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("bot update failed: %r", update, exc_info=context.error)

    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE & new_messages))
    app.add_handler(CommandHandler("bind", bind, filters=filters.ChatType.GROUPS & new_messages))
    app.add_handler(CommandHandler(list(COMMANDS), command, filters=new_messages))
    app.add_handler(CallbackQueryHandler(callback))
    # Separate handler group: topic discovery sees every group message, commands included.
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, group_message), group=1)
    return app
