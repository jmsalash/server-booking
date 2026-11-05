import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.telegram_gateway import MemberStatus, TelegramGateway
from app.models import Booking, LoginToken, Session, User
from app.services import audit, booking, notify
from app.settings_store import SettingsStore

log = logging.getLogger(__name__)

TOKEN_TTL = timedelta(minutes=5)
SESSION_TTL = timedelta(days=30)


@dataclass(frozen=True)
class TgUser:
    id: int
    username: str | None
    display_name: str


class LoginOutcome(StrEnum):
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    NOT_MEMBER = "not_member"
    UNKNOWN_TOKEN = "unknown_token"


@dataclass
class SyncResult:
    deactivated: list[int] = field(default_factory=list)
    admins_changed: int = 0


async def create_login_token(db: AsyncSession, now: datetime) -> str:
    token = secrets.token_urlsafe(32)
    db.add(LoginToken(token=token, created_at=now, expires_at=now + TOKEN_TTL))
    await db.flush()
    return token


async def confirm_login(
    db: AsyncSession, token: str, tg: TgUser, gateway: TelegramGateway, group_id: int, now: datetime
) -> LoginOutcome:
    row = await db.get(LoginToken, token)
    if row is None:
        return LoginOutcome.UNKNOWN_TOKEN
    if now >= row.expires_at or row.consumed_at is not None:
        return LoginOutcome.EXPIRED
    if row.confirmed_at is not None and row.telegram_id != tg.id:
        return LoginOutcome.UNKNOWN_TOKEN
    status = await gateway.get_member_status(group_id, tg.id)
    if status == MemberStatus.NOT_MEMBER:
        return LoginOutcome.NOT_MEMBER
    user = (await db.execute(select(User).where(User.telegram_id == tg.id))).scalar_one_or_none()
    if user is None:
        user = User(telegram_id=tg.id, display_name=tg.display_name, username=tg.username)
        db.add(user)
    user.display_name = tg.display_name
    user.username = tg.username
    user.is_admin = status == MemberStatus.ADMIN
    user.active = True
    user.dm_blocked = False
    user.last_login_at = now
    row.telegram_id = tg.id
    row.confirmed_at = now
    await db.flush()
    return LoginOutcome.CONFIRMED


async def consume_login(db: AsyncSession, token: str, now: datetime) -> str | None:
    row = await db.get(LoginToken, token)
    if (
        row is None
        or row.confirmed_at is None
        or row.consumed_at is not None
        or now >= row.expires_at
    ):
        return None
    user = (
        await db.execute(select(User).where(User.telegram_id == row.telegram_id))
    ).scalar_one_or_none()
    if user is None or not user.active:
        return None
    row.consumed_at = now
    sid = secrets.token_urlsafe(32)
    db.add(Session(id=sid, user_id=user.id, created_at=now, expires_at=now + SESSION_TTL))
    await db.flush()
    return sid


async def session_user(db: AsyncSession, session_id: str, now: datetime) -> User | None:
    s = await db.get(Session, session_id)
    if s is None or s.expires_at <= now:
        return None
    user = await db.get(User, s.user_id)
    if user is None or not user.active:
        return None
    return user


async def sync_membership(
    db: AsyncSession,
    store: SettingsStore,
    gateway: TelegramGateway,
    group_id: int,
    now: datetime,
) -> SyncResult:
    result = SyncResult()
    users = list((await db.execute(select(User).where(User.active).order_by(User.id))).scalars())

    admin_ids = await gateway.get_admin_ids(group_id)
    for u in users:
        is_admin = u.telegram_id in admin_ids
        if u.is_admin != is_admin:
            u.is_admin = is_admin
            result.admins_changed += 1
    await db.flush()

    left: list[User] = []
    for u in users:
        try:
            status = await gateway.get_member_status(group_id, u.telegram_id)
        except Exception:
            log.exception("membership check failed for user %s; skipping", u.id)
            continue
        if status != MemberStatus.NOT_MEMBER:
            continue
        u.active = False
        await db.execute(delete(Session).where(Session.user_id == u.id))
        await db.flush()
        ids = (
            await db.execute(
                select(Booking.id).where(
                    Booking.user_id == u.id, Booking.status == "active", Booking.end_at > now
                ).order_by(Booking.id)
            )
        ).scalars().all()
        for bid in ids:
            await booking.cancel_booking(
                db, store, bid, actor=None, now=now, reason="Left the Telegram group"
            )
        await audit.record(
            db, "user.deactivate", f"user:{u.id}", now=now, details={"telegram_id": u.telegram_id}
        )
        left.append(u)
        result.deactivated.append(u.id)

    if left:
        names = ", ".join(u.display_name for u in left)
        text = f"👋 Deactivated users who left the group: {names}. Their bookings were cancelled."
        admins = (
            await db.execute(select(User).where(User.is_admin, User.active).order_by(User.id))
        ).scalars()
        for admin in admins:
            await notify.dm(db, admin, text, now)
    return result
