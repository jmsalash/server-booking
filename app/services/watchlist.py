from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.types import ServerStatus, StatusCode
from app.models import Server, User, WatchSubscription
from app.services import notify
from app.settings_store import K, SettingsStore


async def subscribe(db: AsyncSession, user_id: int, server_id: int, now: datetime) -> bool:
    """Subscribe; False if already subscribed."""
    inserted = await db.scalar(
        insert(WatchSubscription)
        .values(user_id=user_id, server_id=server_id, created_at=now)
        .on_conflict_do_nothing(constraint="watch_user_server_uq")
        .returning(WatchSubscription.id)
    )
    return inserted is not None


async def unsubscribe(db: AsyncSession, sub_id: int, user_id: int) -> None:
    await db.execute(
        delete(WatchSubscription).where(
            WatchSubscription.id == sub_id, WatchSubscription.user_id == user_id
        )
    )
    await db.flush()


async def for_user(db: AsyncSession, user_id: int) -> list[WatchSubscription]:
    q = select(WatchSubscription).where(WatchSubscription.user_id == user_id)
    return list((await db.scalars(q.order_by(WatchSubscription.id))).all())


async def fire(
    db: AsyncSession, store: SettingsStore, server: Server, status: ServerStatus, now: datetime
) -> int:
    """DM every subscriber of `server` that it is free, drop the subscriptions, return count."""
    rows = (
        await db.execute(
            select(WatchSubscription, User)
            .join(User, User.id == WatchSubscription.user_id)
            .where(WatchSubscription.server_id == server.id)
            .order_by(WatchSubscription.id)
            .with_for_update(of=WatchSubscription)
        )
    ).all()
    if not rows:
        return 0
    portal = await store.get(K.PORTAL_BASE_URL)
    if status.code == StatusCode.SHAREABLE:
        # Someone is on it but lets one more person in: a watcher can join now.
        text = f"🟣 {server.name} is in use, but one more person can join."
    else:
        text = f"🟢 {server.name} is now {status.label}."
    if portal:
        text += f" Book: {portal.rstrip('/')}/book?server_id={server.id}"
    for _sub, user in rows:
        await notify.dm(db, user, text, now)
    await db.execute(
        delete(WatchSubscription).where(WatchSubscription.id.in_([s.id for s, _ in rows]))
    )
    await db.flush()
    return len(rows)
