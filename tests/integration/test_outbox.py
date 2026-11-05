from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import AuditLog, OutboxItem
from app.services import audit, outbox

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


async def test_audit_record(db, make_user):
    u = await make_user()
    await audit.record(db, "x.do", "thing:1", now=NOW, actor_user_id=u.id)
    row = (await db.execute(select(AuditLog))).scalar_one()
    assert (row.action, row.target, row.details, row.actor_user_id) == ("x.do", "thing:1", {}, u.id)
    assert row.ts == NOW


async def test_success_marks_done(db):
    item = await outbox.enqueue(db, "k", {"a": 1}, NOW)
    seen = []

    async def h(_db, payload):
        seen.append(payload)

    n = await outbox.process_due(db, {"k": h}, NOW)
    assert n == 1 and seen == [{"a": 1}]
    assert item.done_at == NOW and item.failed_at is None


async def test_error_backoff_doubles_and_caps(db):
    item = await outbox.enqueue(db, "k", {}, NOW)

    async def h(_db, payload):
        raise RuntimeError("boom")

    now = NOW
    deltas = []
    for _ in range(8):
        assert await outbox.process_due(db, {"k": h}, now) == 1
        deltas.append((item.next_attempt_at - now).total_seconds())
        now = item.next_attempt_at
    assert deltas == [60, 120, 240, 480, 960, 1920, 3600, 3600]
    assert item.attempts == 8 and item.last_error and item.failed_at is None
    # not due before next_attempt_at
    assert await outbox.process_due(db, {"k": h}, now - timedelta(seconds=1)) == 0


async def test_gives_up_after_24h(db):
    item = await outbox.enqueue(db, "k", {}, NOW - timedelta(hours=24))

    async def h(_db, payload):
        raise RuntimeError("boom")

    await outbox.process_due(db, {"k": h}, NOW)
    assert item.failed_at == NOW


async def test_retry_later_does_not_count_attempt(db):
    item = await outbox.enqueue(db, "k", {}, NOW)

    async def h(_db, payload):
        raise outbox.RetryLater(30)

    await outbox.process_due(db, {"k": h}, NOW)
    assert item.attempts == 0
    assert item.next_attempt_at == NOW + timedelta(seconds=30)
    assert item.done_at is None and item.failed_at is None


async def test_permanent_failure(db):
    item = await outbox.enqueue(db, "k", {}, NOW)

    async def h(_db, payload):
        raise outbox.PermanentFailure("nope")

    await outbox.process_due(db, {"k": h}, NOW)
    assert item.failed_at == NOW and item.last_error == "nope"


async def test_items_processed_in_id_order(db):
    for i in range(3):
        await outbox.enqueue(db, "k", {"i": i}, NOW)
    seen = []

    async def h(_db, payload):
        seen.append(payload["i"])

    await outbox.process_due(db, {"k": h}, NOW)
    assert seen == [0, 1, 2]


async def test_unhandled_kind_left_pending(db):
    item = await outbox.enqueue(db, "other", {}, NOW)
    assert await outbox.process_due(db, {"k": None}, NOW) == 0
    assert item.done_at is None and item.failed_at is None


async def test_failing_handler_writes_roll_back(db):
    item = await outbox.enqueue(db, "k", {}, NOW)

    async def h(session, payload):
        await audit.record(session, "side.effect", "t", now=NOW)
        raise RuntimeError("boom")

    await outbox.process_due(db, {"k": h}, NOW)
    assert item.attempts == 1
    assert (await db.execute(select(AuditLog))).first() is None
    assert (await db.execute(select(OutboxItem))).scalar_one().attempts == 1
