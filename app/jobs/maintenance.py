from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LoginToken, OutboxItem, Session
from app.settings_store import SettingsStore

LOGIN_TOKEN_GRACE = timedelta(days=1)
OUTBOX_DONE_KEEP = timedelta(days=7)


async def prune_housekeeping(db: AsyncSession, store: SettingsStore, now: datetime) -> dict:
    """Delete expired login tokens and sessions and long-delivered outbox items."""
    counts = {}
    for name, stmt in (
        ("login_tokens", delete(LoginToken).where(LoginToken.expires_at < now - LOGIN_TOKEN_GRACE)),
        ("sessions", delete(Session).where(Session.expires_at < now)),
        ("outbox_items", delete(OutboxItem).where(OutboxItem.done_at < now - OUTBOX_DONE_KEEP)),
    ):
        counts[name] = (await db.execute(stmt)).rowcount
    return counts
