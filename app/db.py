from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Session as OrmSession

# Postgres channel told about every change open pages should show (see app/web/live.py).
LIVE_CHANNEL = "live"


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _shown(obj, whole: bool) -> bool:
    """Does this flushed row change what the board / calendar show? Models list the columns
    that matter in `__live__`."""
    cols = getattr(type(obj), "__live__", None)
    if not cols:
        return False
    if whole:
        return True
    state = inspect(obj)
    return any(state.attrs[c].history.has_changes() for c in cols)


@event.listens_for(OrmSession, "after_flush")
def _notify_live(session: OrmSession, _ctx) -> None:
    """Tell open pages to refresh. NOTIFY is transactional: it is delivered on commit and
    dropped on rollback, and repeats within one transaction are delivered once. Works from
    every process (web, bot, jobs)."""
    if (any(_shown(o, True) for o in session.new)
            or any(_shown(o, True) for o in session.deleted)
            or any(_shown(o, False) for o in session.dirty)):
        session.connection().execute(text(f"SELECT pg_notify('{LIVE_CHANNEL}', '')"))
