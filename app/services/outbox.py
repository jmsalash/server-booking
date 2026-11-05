from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OutboxItem

Handler = Callable[[AsyncSession, dict], Awaitable[None]]

GIVE_UP_AFTER = timedelta(hours=24)


class RetryLater(Exception):
    def __init__(self, seconds: float):
        super().__init__(f"retry in {seconds}s")
        self.seconds = seconds


class PermanentFailure(Exception):
    pass


async def enqueue(db: AsyncSession, kind: str, payload: dict, now: datetime) -> OutboxItem:
    item = OutboxItem(kind=kind, payload=payload, created_at=now, next_attempt_at=now)
    db.add(item)
    await db.flush()
    return item


async def process_due(
    db: AsyncSession, handlers: Mapping[str, Handler], now: datetime, limit: int = 50
) -> int:
    if not handlers:
        return 0
    rows = await db.execute(
        select(OutboxItem)
        .where(
            OutboxItem.done_at.is_(None),
            OutboxItem.failed_at.is_(None),
            OutboxItem.next_attempt_at <= now,
            OutboxItem.kind.in_(list(handlers)),
        )
        .order_by(OutboxItem.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    items = list(rows.scalars())
    for item in items:
        handler = handlers[item.kind]
        try:
            async with db.begin_nested():
                await handler(db, item.payload)
            item.done_at = now
        except RetryLater as e:
            item.next_attempt_at = now + timedelta(seconds=e.seconds)
        except PermanentFailure as e:
            item.last_error = str(e)
            item.failed_at = now
        except Exception as e:  # noqa: BLE001 - any handler error is retried
            item.attempts += 1
            item.last_error = f"{type(e).__name__}: {e}"
            item.next_attempt_at = now + timedelta(
                seconds=min(60 * 2 ** (item.attempts - 1), 3600)
            )
            if now - item.created_at >= GIVE_UP_AFTER:
                item.failed_at = now
        await db.flush()
    return len(items)
