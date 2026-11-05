import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import utcnow

log = logging.getLogger(__name__)


async def run_job(
    name: str,
    fn: Callable[[AsyncSession, datetime], Awaitable[Any]],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Run one job in its own transaction; a failure is logged and rolled back, never raised."""
    async with sessionmaker() as db:
        try:
            await fn(db, utcnow())
            await db.commit()
        except Exception:
            log.exception("job %s failed", name)
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001  e.g. the connection died; the job stays contained
                log.exception("rollback after job %s failed", name)
