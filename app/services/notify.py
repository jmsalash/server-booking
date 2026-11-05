from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.telegram_gateway import BotBlocked, TelegramGateway
from app.models import User
from app.services import outbox
from app.settings_store import K, SettingsStore

KIND = "telegram_message"
TOPIC_KINDS = ("new_booking", "reclaimed", "freed")


def _payload(chat_id, text, thread_id, buttons) -> dict:
    return {
        "chat_id": chat_id,
        "text": text,
        "thread_id": thread_id,
        "buttons": [[label, data] for label, data in buttons] if buttons else None,
    }


async def dm(
    db: AsyncSession, user: User, text: str, now: datetime, buttons=None
) -> None:
    if user.dm_blocked or not user.active:
        return
    await outbox.enqueue(db, KIND, _payload(user.telegram_id, text, None, buttons), now)


async def post_topic(
    db: AsyncSession, settings: SettingsStore, kind: str, text: str, now: datetime
) -> None:
    group = await settings.get(K.TG_GROUP_ID)
    topic = await settings.get(K.TG_TOPIC_ID)
    if group is None or topic is None:
        return
    toggles = await settings.get(K.TG_TOPIC_POSTS) or {}
    if not toggles.get(kind, True):
        return
    await outbox.enqueue(db, KIND, _payload(group, text, topic, None), now)


def make_telegram_handler(gateway: TelegramGateway) -> outbox.Handler:
    async def handler(db: AsyncSession, payload: dict) -> None:
        buttons = [(b[0], b[1]) for b in payload["buttons"]] if payload.get("buttons") else None
        try:
            await gateway.send_message(
                payload["chat_id"], payload["text"], payload.get("thread_id"), buttons
            )
        except BotBlocked as e:
            if payload.get("thread_id") is not None:
                raise outbox.PermanentFailure(f"bot blocked in chat {payload['chat_id']}") from e
            res = await db.execute(
                update(User).where(User.telegram_id == payload["chat_id"]).values(dm_blocked=True)
            )
            if res.rowcount == 0:
                raise outbox.PermanentFailure(f"bot blocked in chat {payload['chat_id']}") from e

    return handler
