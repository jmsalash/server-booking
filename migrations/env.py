import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401
from app.db import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("Set DATABASE_URL or sqlalchemy.url")
    return url


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_async())
