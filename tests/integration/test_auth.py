from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.integrations.telegram_gateway import MemberStatus
from app.models import AuditLog, Booking, Session
from app.services.auth import (
    LoginOutcome, TgUser, confirm_login, consume_login, create_login_token,
    session_user, sync_membership,
)
from tests.fakes import FakeTelegram

G = -100123
now = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
tg = TgUser(id=555, username="ana", display_name="Ana Rossi")


def _tg(**kw):
    return FakeTelegram(members={555: MemberStatus.MEMBER}, **kw)


async def test_member_login_flow(db):
    fake = _tg()
    t = await create_login_token(db, now)
    assert await confirm_login(db, t, tg, fake, G, now) == LoginOutcome.CONFIRMED
    sid = await consume_login(db, t, now)
    u = await session_user(db, sid, now)
    assert u.telegram_id == tg.id and u.display_name == "Ana Rossi" and not u.is_admin
    assert await session_user(db, sid, now + timedelta(days=31)) is None
    u.active = False
    assert await session_user(db, sid, now) is None


async def test_token_single_use(db):
    t = await create_login_token(db, now)
    await confirm_login(db, t, tg, _tg(), G, now)
    assert await consume_login(db, t, now)
    assert await consume_login(db, t, now) is None
    assert await confirm_login(db, t, tg, _tg(), G, now) == LoginOutcome.EXPIRED


async def test_unconfirmed_token_not_consumable(db):
    t = await create_login_token(db, now)
    assert await consume_login(db, t, now) is None
    assert await confirm_login(db, "nope", tg, _tg(), G, now) == LoginOutcome.UNKNOWN_TOKEN


async def test_expired_token(db):
    t = await create_login_token(db, now)
    late = now + timedelta(minutes=6)
    assert await confirm_login(db, t, tg, _tg(), G, late) == LoginOutcome.EXPIRED
    assert await consume_login(db, t, late) is None


async def test_non_member_rejected(db):
    t = await create_login_token(db, now)
    assert await confirm_login(db, t, tg, FakeTelegram(), G, now) == LoginOutcome.NOT_MEMBER
    assert await consume_login(db, t, now) is None


async def test_other_user_cannot_hijack_confirmed_token(db):
    t = await create_login_token(db, now)
    await confirm_login(db, t, tg, _tg(), G, now)
    other = TgUser(id=556, username=None, display_name="Eve")
    fake = FakeTelegram(members={556: MemberStatus.MEMBER})
    assert await confirm_login(db, t, other, fake, G, now) == LoginOutcome.UNKNOWN_TOKEN
    assert await confirm_login(db, t, tg, _tg(), G, now) == LoginOutcome.CONFIRMED


async def test_group_admin_gets_is_admin(db):
    fake = FakeTelegram(members={555: MemberStatus.ADMIN})
    t = await create_login_token(db, now)
    await confirm_login(db, t, tg, fake, G, now)
    assert (await session_user(db, await consume_login(db, t, now), now)).is_admin


async def test_sync_deactivates_leaver_and_cancels_bookings(db, store, make_user, make_server):
    leaver = await make_user(telegram_id=1, display_name="Leaver")
    stay = await make_user(telegram_id=2, display_name="Stay", is_admin=True)
    srv = await make_server()
    db.add(Session(id="s1", user_id=leaver.id, expires_at=now + timedelta(days=1)))
    b = Booking(user_id=leaver.id, server_id=srv.id, start_at=now + timedelta(hours=2),
                end_at=now + timedelta(hours=4), resource="cpu")
    db.add(b)
    await db.flush()
    fake = FakeTelegram(members={2: MemberStatus.ADMIN}, admins={2})
    res = await sync_membership(db, store, fake, G, now)
    assert res.deactivated == [leaver.id] and res.admins_changed == 0
    await db.refresh(leaver); await db.refresh(b)
    assert not leaver.active and b.status == "cancelled"
    assert b.cancel_reason == "Left the Telegram group"
    assert await db.get(Session, "s1") is None
    audits = (await db.execute(select(AuditLog.action))).scalars().all()
    assert "user.deactivate" in audits
    from app.models import OutboxItem  # noqa
    items = (await db.execute(select(OutboxItem))).scalars().all()
    texts = [i.payload["text"] for i in items]
    assert texts == [
        "👋 Deactivated users who left the group: Leaver. Their bookings were cancelled."
    ]
    assert items[0].payload["chat_id"] == 2


async def test_sync_revokes_admin(db, store, make_user):
    u = await make_user(telegram_id=7, is_admin=True)
    v = await make_user(telegram_id=8)
    fake = FakeTelegram(
        members={7: MemberStatus.MEMBER, 8: MemberStatus.ADMIN}, admins={8}
    )
    res = await sync_membership(db, store, fake, G, now)
    assert res.admins_changed == 2 and res.deactivated == []
    await db.refresh(u); await db.refresh(v)
    assert not u.is_admin and v.is_admin


async def test_sync_gateway_error_skips_user(db, store, make_user):
    a = await make_user(telegram_id=1)
    b = await make_user(telegram_id=2)

    class Flaky(FakeTelegram):
        async def get_member_status(self, chat_id, user_id):
            if user_id == 1:
                raise RuntimeError("boom")
            return await super().get_member_status(chat_id, user_id)

    res = await sync_membership(db, store, Flaky(), G, now)
    await db.refresh(a); await db.refresh(b)
    assert res.deactivated == [b.id] and a.active and not b.active
