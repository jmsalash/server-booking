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


async def test_orm_and_migrated_schema_table_names_agree(db):
    rows = await db.execute(text(
        "select table_name from information_schema.tables where table_schema='public'"))
    live = {r[0] for r in rows} - {"alembic_version"}
    assert live == {t.name for t in Base.metadata.sorted_tables}
