from enum import StrEnum
from typing import NamedTuple, Protocol

import telegram
from telegram.error import Forbidden, RetryAfter

from app.services.outbox import RetryLater


class BotBlocked(Exception):
    """The recipient blocked the bot (or the bot may not post there)."""


class BotIdentity(NamedTuple):
    id: int
    username: str


class MemberStatus(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"
    NOT_MEMBER = "not_member"


class TelegramGateway(Protocol):
    async def get_me(self) -> BotIdentity: ...

    async def get_member_status(self, chat_id: int, user_id: int) -> MemberStatus: ...

    async def get_admin_ids(self, chat_id: int) -> set[int]: ...

    async def send_message(
        self,
        chat_id: int,
        text: str,
        thread_id: int | None = None,
        buttons: list[tuple[str, str]] | None = None,
    ) -> None: ...


class PtbGateway:
    def __init__(self, bot: telegram.Bot):
        self.bot = bot

    async def get_me(self) -> BotIdentity:
        me = await self.bot.get_me()
        return BotIdentity(me.id, me.username)

    async def get_member_status(self, chat_id: int, user_id: int) -> MemberStatus:
        m = await self.bot.get_chat_member(chat_id, user_id)
        if m.status in ("creator", "administrator"):
            return MemberStatus.ADMIN
        if m.status == "member" or (m.status == "restricted" and m.is_member):
            return MemberStatus.MEMBER
        return MemberStatus.NOT_MEMBER

    async def get_admin_ids(self, chat_id: int) -> set[int]:
        return {m.user.id for m in await self.bot.get_chat_administrators(chat_id)}

    async def send_message(self, chat_id, text, thread_id=None, buttons=None) -> None:
        markup = None
        if buttons:
            markup = telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton(label, callback_data=data)] for label, data in buttons]
            )
        try:
            await self.bot.send_message(
                chat_id, text, message_thread_id=thread_id, reply_markup=markup
            )
        except RetryAfter as e:
            ra = e.retry_after
            raise RetryLater(ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)) from e
        except Forbidden as e:
            raise BotBlocked(str(e)) from e
