from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.booking_rules import BookingError, BookingRequest, validate_booking
from app.domain.rules import Rules
from app.domain.status import format_day_hm
from app.domain.timeutil import floor_hour
from app.domain.types import BookingView
from app.models import Booking, Server, User
from app.services import audit, board, calendar_sync, notify
from app.services.inventory import to_info
from app.settings_store import SettingsStore

OVERLAP_CONSTRAINT = "bookings_no_overlap"


class BookingRejected(Exception):
    def __init__(self, error: BookingError, host: BookingView | None = None):
        super().__init__(str(error))
        self.error = error
        self.host = host  # OUTSIDE_HOST: the main booking the time is borrowed from


class ShareConfirmationRequired(Exception):
    """The booking would join these shareable bookings; ask the booker to confirm first."""

    def __init__(self, share_with: tuple[BookingView, ...]):
        super().__init__("booking would be shared with "
                         + ", ".join(b.user_name for b in share_with))
        self.share_with = share_with


def _fmt_span(start: datetime, end: datetime, rules: Rules) -> str:
    tz = rules.timezone
    return f"{format_day_hm(start, tz)}–{format_day_hm(end, tz)}"


def _span(b: Booking, rules: Rules) -> str:
    return _fmt_span(b.start_at, b.end_at, rules)


async def _tell_joined(
    db: AsyncSession, joiner: User, server: Server, start: datetime, end: datetime,
    joined: tuple[BookingView, ...], now: datetime, rules: Rules,
) -> None:
    """DM the owner of each shareable booking someone has just joined."""
    for x in joined:
        owner = await db.get(User, x.user_id)
        span = _fmt_span(max(start, x.start), min(end, x.end), rules)
        await notify.dm(
            db, owner, f"🤝 {joiner.display_name} will share {server.name} with you {span}.", now
        )


def _target(b: Booking) -> str:
    return f"booking:{b.id}"


def _check_actor(b: Booking, actor: User | None) -> None:
    if actor is None:  # system action
        return
    if actor.id != b.user_id and not actor.is_admin:
        raise PermissionError(f"user {actor.id} may not modify booking {b.id}")


async def _lock_server(db: AsyncSession, server_id: int) -> Server:
    server = await db.scalar(
        select(Server).where(Server.id == server_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if server is None:
        raise LookupError(f"server {server_id} not found")
    return server


async def _lock_booking(db: AsyncSession, booking_id: int) -> Booking:
    b = await db.scalar(
        select(Booking).where(Booking.id == booking_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if b is None:
        raise LookupError(f"booking {booking_id} not found")
    return b


async def _flush_or_conflict(db: AsyncSession, add: Booking | None = None) -> None:
    """Flush inside a savepoint; an exclusion-constraint violation becomes CONFLICT."""
    try:
        async with db.begin_nested():
            if add is not None:
                db.add(add)
            await db.flush()
    except IntegrityError as e:
        if OVERLAP_CONSTRAINT in str(e.orig):
            raise BookingRejected(BookingError.CONFLICT) from e
        raise


def _ceil_hour(now: datetime, tz: str) -> datetime:
    floor = floor_hour(now, tz)
    return floor if floor == now else floor + timedelta(hours=1)


async def create_booking(
    db: AsyncSession,
    store: SettingsStore,
    user: User,
    server_id: int,
    start: datetime,
    end: datetime,
    resource: str,
    now: datetime,
    confirm_shared: bool = False,
    shareable: bool = False,
) -> Booking:
    """Book a server. Joining a shareable booking is allowed once the booker confirms: the new
    booking is the partner (never itself shareable) and must lie inside the shareable booking.
    Any other overlap is rejected."""
    if not user.active:
        raise PermissionError(f"user {user.id} is inactive")
    rules = await store.get_rules()
    server = await _lock_server(db, server_id)
    if server.archived_at is not None:
        raise LookupError(f"server {server_id} not found")
    existing = await board.active_bookings(db, server_id, now)
    v = validate_booking(
        BookingRequest(start, end, resource, user.id), existing, to_info(server), now, rules
    )
    if not v.ok:
        raise BookingRejected(v.error, v.host)
    if v.is_shared and not confirm_shared:
        raise ShareConfirmationRequired(v.share_with)

    b = Booking(
        user_id=user.id, server_id=server_id, start_at=start, end_at=end,
        resource=resource, status="active", is_shared=v.is_shared,
        shareable=shareable and not v.is_shared,  # at most two people
    )
    await _flush_or_conflict(db, add=b)
    await _tell_joined(db, user, server, start, end, v.share_with, now, rules)

    await calendar_sync.queue_create(db, store, b.id, now)
    text = f"🔴 {server.name} booked by {user.display_name}, {_span(b, rules)}"
    if b.is_shared:
        text += " (shared)"
    await notify.post_topic(db, store, "new_booking", text, now)
    await audit.record(
        db, "booking.create", _target(b), now=now, actor_user_id=user.id,
        details={"server_id": server_id, "start": start.isoformat(), "end": end.isoformat(),
                 "resource": resource, "is_shared": b.is_shared, "shareable": b.shareable,
                 "shared_with": [x.id for x in v.share_with]},
    )
    return b


async def _end_early(
    db: AsyncSession, store: SettingsStore, b: Booking, now: datetime, rules: Rules
) -> dict | None:
    """Shorten an ongoing booking to the next hour boundary. Returns audit details or None."""
    new_end = _ceil_hour(now, rules.timezone)
    old_end = b.end_at
    if new_end >= old_end:
        return None
    if new_end <= b.start_at:
        b.status = "cancelled"
        b.cancelled_at = now
        await db.flush()
        await calendar_sync.queue_delete(db, store, b.id, now)
    else:
        b.end_at = new_end
        # The new end is at most an hour away: an "ends soon / Extend" reminder right after
        # the user chose to end early would be noise.
        b.end_reminder_sent = True
        await db.flush()
        await calendar_sync.queue_update(db, store, b.id, now)
    return {"old_end": old_end.isoformat(), "new_end": new_end.isoformat(), "status": b.status}


async def cancel_booking(
    db: AsyncSession,
    store: SettingsStore,
    booking_id: int,
    actor: User | None,
    now: datetime,
    reason: str | None = None,
    immediate: bool = False,
) -> None:
    """Cancel a future booking; an ongoing one ends early at the next hour boundary.

    With `immediate` (the server is being deleted) an ongoing booking is cancelled outright too.
    """
    b = await _lock_booking(db, booking_id)
    _check_actor(b, actor)
    if b.status != "active" or b.end_at <= now:
        return
    rules = await store.get_rules()
    span = _span(b, rules)
    if b.start_at > now or immediate:
        b.status = "cancelled"
        b.cancelled_at = now
        b.cancel_reason = reason
        await db.flush()
        await calendar_sync.queue_delete(db, store, b.id, now)
        details = {"mode": "cancel"}
    else:
        ended = await _end_early(db, store, b, now, rules)
        if ended is None:
            return
        b.cancel_reason = reason
        await db.flush()
        details = {"mode": "end_early", **ended}
    if reason:
        details["reason"] = reason

    if actor is None or actor.id != b.user_id:
        owner = await db.get(User, b.user_id)
        server = await db.get(Server, b.server_id)
        text = f"❌ Your booking on {server.name} ({span}) was cancelled by an admin."
        if reason:
            text += f"\nReason: {reason}"
        await notify.dm(db, owner, text, now)
    await audit.record(
        db, "booking.cancel", _target(b), now=now,
        actor_user_id=actor.id if actor else None, details=details,
    )


async def end_early(
    db: AsyncSession, store: SettingsStore, booking_id: int, actor: User | None, now: datetime
) -> None:
    b = await _lock_booking(db, booking_id)
    _check_actor(b, actor)
    if b.status != "active" or b.end_at <= now:
        return
    rules = await store.get_rules()
    details = await _end_early(db, store, b, now, rules)
    if details is None:
        return
    await audit.record(
        db, "booking.end_early", _target(b), now=now,
        actor_user_id=actor.id if actor else None, details=details,
    )


async def extend(
    db: AsyncSession,
    store: SettingsStore,
    booking_id: int,
    actor: User,
    new_end: datetime,
    now: datetime,
) -> None:
    current = await db.get(Booking, booking_id)
    if current is None:
        raise LookupError(f"booking {booking_id} not found")
    server = await _lock_server(db, current.server_id)  # serialise with creates
    b = await _lock_booking(db, booking_id)
    _check_actor(b, actor)
    if not actor.active and not actor.is_admin:
        raise PermissionError(f"user {actor.id} is inactive")
    if b.status != "active" or b.end_at <= now:
        raise BookingRejected(BookingError.IN_PAST)
    if new_end <= b.end_at:
        raise BookingRejected(BookingError.END_NOT_AFTER_START)
    rules = await store.get_rules()
    # The booking may already be running: validate from the current hour so IN_PAST does not
    # fire, but enforce the maximum length from the original start.
    if new_end - b.start_at > timedelta(hours=rules.max_booking_hours):
        raise BookingRejected(BookingError.TOO_LONG)
    req_start = max(b.start_at, floor_hour(now, rules.timezone))
    # Extending never joins anyone: running into another booking is a conflict. A partner only
    # borrows time inside the main booking it joined, so it can't extend past that booking.
    existing = [x for x in await board.active_bookings(db, b.server_id, now) if x.id != b.id]
    mine = board.to_view(b, "")
    overlapping = board.overlapping(mine, existing)
    if b.is_shared:
        host = next((x for x in overlapping if not x.is_shared), None)
        if host is None or new_end > host.end:
            raise BookingRejected(BookingError.OUTSIDE_HOST, host)
        paired = frozenset({host.id})
    else:
        paired = frozenset(x.id for x in overlapping if x.is_shared)
    v = validate_booking(
        BookingRequest(req_start, new_end, b.resource, b.user_id), existing, to_info(server),
        now, rules, paired, may_join=False,
    )
    if not v.ok:
        raise BookingRejected(v.error, v.host)
    old_end, old_reminder = b.end_at, b.end_reminder_sent
    b.end_at = new_end
    b.end_reminder_sent = False  # re-arm the end reminder for the new end
    try:
        await _flush_or_conflict(db)
    except BookingRejected:
        b.end_at, b.end_reminder_sent = old_end, old_reminder
        raise
    await calendar_sync.queue_update(db, store, b.id, now)
    await audit.record(
        db, "booking.extend", _target(b), now=now, actor_user_id=actor.id,
        details={"old_end": old_end.isoformat(), "new_end": new_end.isoformat()},
    )


async def reclaim(db: AsyncSession, store: SettingsStore, booking_id: int, now: datetime) -> None:
    b = await _lock_booking(db, booking_id)
    if b.status != "active" or not (b.start_at < now < b.end_at):
        return
    rules = await store.get_rules()
    b.status = "reclaimed"
    b.end_at = now
    await db.flush()
    await calendar_sync.queue_delete(db, store, b.id, now)
    owner = await db.get(User, b.user_id)
    server = await db.get(Server, b.server_id)
    n = rules.max_idle_warnings
    await notify.dm(
        db, owner,
        f"♻️ Your booking on {server.name} was released because it was idle "
        f"(no activity after {n} warnings).",
        now,
    )
    await notify.post_topic(
        db, store, "reclaimed",
        f"♻️ {server.name} booking by {owner.display_name} was released after {n} idle warnings.",
        now,
    )
    await audit.record(db, "booking.reclaim", _target(b), now=now, details={"idle_warnings": n})


async def set_shareable(
    db: AsyncSession, booking_id: int, actor: User, shareable: bool, now: datetime
) -> bool:
    """Let others join this booking (or stop letting them). Returns whether it changed.

    Refused (SHARE_LOCKED) while the booking is shared: a partner can't open it to a third
    person, and the owner can't take the place back from someone already in it.
    """
    current = await db.get(Booking, booking_id)
    if current is None:
        raise LookupError(f"booking {booking_id} not found")
    await _lock_server(db, current.server_id)  # serialise with joins
    b = await _lock_booking(db, booking_id)
    _check_actor(b, actor)
    if b.status != "active" or b.end_at <= now:
        raise BookingRejected(BookingError.IN_PAST)
    if b.shareable == shareable:
        return False
    others = await board.active_bookings(db, b.server_id, now)
    if b.is_shared or board.overlapping(board.to_view(b, ""), others):
        raise BookingRejected(BookingError.SHARE_LOCKED)
    b.shareable = shareable
    await db.flush()
    await audit.record(
        db, "booking.shareable", _target(b), now=now, actor_user_id=actor.id,
        details={"shareable": shareable},
    )
    return True


async def acknowledge_still_using(
    db: AsyncSession, booking_id: int, telegram_id: int, now: datetime
) -> bool:
    row = (
        await db.execute(
            select(Booking, User.telegram_id)
            .join(User, User.id == Booking.user_id)
            .where(Booking.id == booking_id)
            .with_for_update(of=Booking)
        )
    ).first()
    if row is None:
        return False
    b, owner_tg = row
    if owner_tg != telegram_id or b.status != "active" or b.end_at <= now:
        return False
    b.idle_warnings = 0
    b.idle_since = None
    b.idle_ram_baseline = None
    await db.flush()
    return True


async def user_bookings(db: AsyncSession, user_id: int, now: datetime) -> list[BookingView]:
    rows = await db.execute(
        select(Booking, User.display_name)
        .join(User, User.id == Booking.user_id)
        .where(Booking.user_id == user_id, Booking.status == "active", Booking.end_at > now)
        .order_by(Booking.start_at, Booking.id)
    )
    return [board.to_view(b, name) for b, name in rows.all()]


async def partners(
    db: AsyncSession, bookings: list[BookingView], now: datetime
) -> dict[int, list[BookingView]]:
    """For each booking, the other active bookings it shares the machine with."""
    by_server: dict[int, list[BookingView]] = {}
    out: dict[int, list[BookingView]] = {}
    for b in bookings:
        if b.server_id not in by_server:
            by_server[b.server_id] = await board.active_bookings(db, b.server_id, now)
        out[b.id] = board.overlapping(b, by_server[b.server_id])
    return out
