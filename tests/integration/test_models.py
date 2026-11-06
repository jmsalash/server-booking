from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import Base
from app.models import Booking


def T(h):
    return datetime(2026, 11, 10, h, tzinfo=UTC)


def _b(u, s, start, end, **kw):
    return Booking(user_id=u.id, server_id=s.id, start_at=T(start), end_at=T(end),
                   resource="cpu", status=kw.pop("status", "active"), **kw)


async def test_overlapping_unshared_active_bookings_rejected(db, make_user, make_server):
    u, s = await make_user(), await make_server()
    db.add(_b(u, s, 10, 14))
    await db.flush()
    db.add(_b(u, s, 13, 15))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_shared_or_cancelled_bookings_may_overlap(db, make_user, make_server):
    u, s = await make_user(), await make_server()
    db.add(_b(u, s, 10, 14))
    await db.flush()
    db.add(_b(u, s, 13, 15, is_shared=True))
    await db.flush()
    db.add(_b(u, s, 12, 16, status="cancelled"))
    await db.flush()


async def test_adjacent_bookings_allowed(db, make_user, make_server):
    u, s = await make_user(), await make_server()
    db.add(_b(u, s, 10, 14))
    db.add(_b(u, s, 14, 16))
    await db.flush()


async def test_end_must_be_after_start(db, make_user, make_server):
    u, s = await make_user(), await make_server()
    db.add(_b(u, s, 10, 10))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_orm_and_migrated_schema_agree(db):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    def diff(sync_conn):
        ctx = MigrationContext.configure(sync_conn)
        return compare_metadata(ctx, Base.metadata)

    conn = await db.connection()
    result = await conn.run_sync(diff)
    assert result == []


async def test_live_server_names_unique_but_archived_names_reusable(db, make_server):
    old = await make_server(name="gpu-01")
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await make_server(name="gpu-01")
    old.archived_at = T(9)
    await db.flush()
    await make_server(name="gpu-01")  # the archived one no longer holds the name
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            old.archived_at = None  # restoring would make two live "gpu-01"
            await db.flush()


async def test_partner_cannot_be_shareable(db, make_user, make_server):
    u, s = await make_user(), await make_server()
    db.add(_b(u, s, 10, 14, is_shared=True, shareable=True))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_migration_0003_moves_sharing_to_bookings(pg_url):
    """Upgrading a 0002 database keeps its bookings, all non-shareable, and drops the flag."""
    import asyncio
    import uuid

    from sqlalchemy.ext.asyncio import create_async_engine

    from tests.conftest import _alembic

    name = f"mig_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = pg_url.rsplit("/", 1)[0] + "/" + name
    engine = create_async_engine(url)
    try:
        await asyncio.to_thread(_alembic, url, "upgrade", "0002")
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO users (id, telegram_id, display_name) VALUES (1, 1, 'Ana')"))
            await conn.execute(text(
                "INSERT INTO servers (id, name, hostname, kind, shareable) "
                "VALUES (1, 'gpu-01', 'gpu-01.lab', 'grafana', true)"))
            await conn.execute(text(
                "INSERT INTO bookings (user_id, server_id, start_at, end_at, resource, is_shared) "
                "VALUES (1, 1, '2026-11-10 10:00Z', '2026-11-10 14:00Z', 'cpu', false), "
                "       (1, 1, '2026-11-10 11:00Z', '2026-11-10 12:00Z', 'cpu', true)"))
        await asyncio.to_thread(_alembic, url, "upgrade", "head")
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT shareable FROM bookings"))).all()
            assert [r[0] for r in rows] == [False, False]
            cols = (await conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'servers'"))).scalars().all()
            assert "shareable" not in cols
        await asyncio.to_thread(_alembic, url, "downgrade", "0002")
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT shareable FROM servers"))).scalar() is False
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()
