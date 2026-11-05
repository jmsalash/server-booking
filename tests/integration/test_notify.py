from datetime import datetime, timezone

from sqlalchemy import select

from app.integrations.telegram_gateway import BotBlocked
from app.models import OutboxItem
from app.services import notify, outbox
from app.settings_store import K
from tests.fakes import FakeTelegram

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


async def _items(db):
    return (await db.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars().all()


async def test_dm_enqueues(db, make_user):
    u = await make_user()
    await notify.dm(db, u, "hi", NOW, buttons=[("OK", "cb")])
    [it] = await _items(db)
    assert it.kind == "telegram_message"
    assert it.payload == {
        "chat_id": u.telegram_id, "text": "hi", "thread_id": None, "buttons": [["OK", "cb"]],
    }


async def test_dm_skipped_when_blocked(db, make_user):
    u = await make_user(dm_blocked=True)
    await notify.dm(db, u, "hi", NOW)
    assert await _items(db) == []


async def test_dm_skipped_when_inactive(db, make_user):
    u = await make_user(active=False)
    await notify.dm(db, u, "hi", NOW)
    assert await _items(db) == []


async def test_blocked_bot_sets_flag(db, make_user):
    u = await make_user()
    await notify.dm(db, u, "hi", NOW)
    fake = FakeTelegram(fail_with=BotBlocked())
    n = await outbox.process_due(
        db, {"telegram_message": notify.make_telegram_handler(fake)}, NOW
    )
    assert n == 1
    await db.refresh(u)
    assert u.dm_blocked
    [it] = await _items(db)
    assert it.done_at == NOW


async def test_handler_sends(db, make_user):
    u = await make_user()
    await notify.dm(db, u, "hi", NOW)
    fake = FakeTelegram()
    await outbox.process_due(db, {"telegram_message": notify.make_telegram_handler(fake)}, NOW)
    assert fake.sent == [
        {"chat_id": u.telegram_id, "text": "hi", "thread_id": None, "buttons": None}
    ]


async def test_topic_post_blocked_is_permanent(db, store):
    await store.set(K.TG_GROUP_ID, -100)
    await store.set(K.TG_TOPIC_ID, 7)
    await notify.post_topic(db, store, "freed", "x", NOW)
    fake = FakeTelegram(fail_with=BotBlocked())
    await outbox.process_due(db, {"telegram_message": notify.make_telegram_handler(fake)}, NOW)
    [it] = await _items(db)
    assert it.failed_at == NOW


async def test_topic_post_respects_toggle(db, store):
    await store.set(K.TG_GROUP_ID, -100)
    await store.set(K.TG_TOPIC_ID, 7)
    await store.set(K.TG_TOPIC_POSTS, {"new_booking": False, "reclaimed": True, "freed": True})
    await notify.post_topic(db, store, "new_booking", "x", NOW)
    assert await _items(db) == []
    await notify.post_topic(db, store, "freed", "y", NOW)
    assert len(await _items(db)) == 1


async def test_topic_post_noop_when_unset(db, store):
    await notify.post_topic(db, store, "freed", "x", NOW)
    assert await _items(db) == []


async def test_topic_post_uses_thread_id(db, store):
    await store.set(K.TG_GROUP_ID, -100)
    await store.set(K.TG_TOPIC_ID, 7)
    await notify.post_topic(db, store, "reclaimed", "x", NOW)
    [it] = await _items(db)
    assert it.payload["chat_id"] == -100 and it.payload["thread_id"] == 7
