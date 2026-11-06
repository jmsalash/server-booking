from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import format
from app.domain.idle import IdleAction, IdleState, evaluate_machine_idle
from app.domain.readings import FullState, evaluate_full
from app.models import Booking, Server, User
from app.services import booking as booking_svc
from app.services import notify, usage
from app.settings_store import SettingsStore


async def _active_now(
    db: AsyncSession, now: datetime, lock: bool
) -> list[tuple[int, list[Booking]]]:
    """Active bookings running at `now` on enabled, live Grafana servers, grouped by server id.

    Lock order (shared with the booking service and booking_lifecycle): server rows first, then
    booking rows by id. With `lock`, the bookings are locked in id order in one statement so a
    concurrent "still using it" acknowledgement is not overwritten; grouping happens here.
    """
    q = (
        select(Booking)
        .join(Server, Server.id == Booking.server_id)
        .where(
            Booking.status == "active", Booking.start_at <= now, Booking.end_at > now,
            Server.enabled.is_(True), Server.kind == "grafana", Server.archived_at.is_(None),
        )
        .order_by(Booking.id)
        .execution_options(populate_existing=True)
    )
    if lock:
        q = q.with_for_update(of=Booking)
    by_server: dict[int, list[Booking]] = defaultdict(list)
    for b in await db.scalars(q):
        by_server[b.server_id].append(b)
    return sorted(by_server.items())


async def idle_check(
    db: AsyncSession, store: SettingsStore, now: datetime
) -> list[tuple[int, IdleAction]]:
    """Advance idle tracking for every running booking.

    The machine counts as quiet only when CPU and GPUs are all quiet, whatever was booked. On
    a shared machine each owner is warned with their own "I'm still using it" button, and only a booking
    that ran out of warnings is reclaimed — the other keeps the machine.
    """
    rules = await store.get_rules()
    actions: list[tuple[int, IdleAction]] = []
    for server_id, bookings in await _active_now(db, now, lock=True):
        states = [IdleState(b.idle_warnings, b.idle_since, b.idle_ram_baseline) for b in bookings]
        latest = await usage.latest(db, server_id)
        decisions = evaluate_machine_idle(states, latest, now, rules)
        for b, decision in zip(bookings, decisions):
            b.idle_warnings = decision.state.warnings
            b.idle_since = decision.state.idle_since
            b.idle_ram_baseline = decision.state.ram_baseline
            if decision.action == IdleAction.WARN:
                b.last_warning_at = now
        await db.flush()
        server = await db.get(Server, server_id)
        for b, decision in zip(bookings, decisions):
            if decision.action == IdleAction.WARN:
                owner = await db.get(User, b.user_id)
                text = format.idle_warning(
                    server.name, decision.warning_number, rules.max_idle_warnings,
                    rules.idle_window_min,
                )
                await notify.dm(db, owner, text, now,
                                buttons=[("I'm still using it", f"still:{b.id}")])
            elif decision.action == IdleAction.RECLAIM:
                # Bookings are already locked (in id order) above; reclaim re-locks the same row.
                await booking_svc.reclaim(db, store, b.id, now)
            if decision.action != IdleAction.NONE:
                actions.append((b.id, decision.action))
    return actions


async def full_check(db: AsyncSession, store: SettingsStore, now: datetime) -> int:
    """Warn the people using a machine when its RAM is almost full, or a disk becomes so.

    Runs at any hour. Each booking remembers what it was warned about, so a warning is
    repeated only after the value dropped back (see readings.evaluate_full). Only the
    bookings whose state changes are locked. Returns the number of messages queued.
    """
    rules = await store.get_rules()
    changed: dict[int, FullState] = {}
    sends: list[tuple[int, str]] = []  # (user id, text)
    for server_id, bookings in await _active_now(db, now, lock=False):
        latest = await usage.latest(db, server_id)
        server = await db.get(Server, server_id)
        for b in bookings:
            disks = frozenset(b.disk_alerted) if b.disk_alerted is not None else None
            state = FullState(b.ram_alerted_at is not None, disks)
            d = evaluate_full(state, latest, now, rules)
            if d.state != state:
                changed[b.id] = d.state
            if d.ram_pct is not None:
                sends.append((b.user_id, format.ram_full(server.name, d.ram_pct)))
            if d.disks:
                sends.append((b.user_id, format.disk_full(server.name, d.disks)))
    if changed:
        locked = await db.scalars(
            select(Booking).where(Booking.id.in_(changed)).order_by(Booking.id)
            .with_for_update().execution_options(populate_existing=True)
        )
        for b in locked:
            st = changed[b.id]
            if not st.ram:
                b.ram_alerted_at = None
            elif b.ram_alerted_at is None:
                b.ram_alerted_at = now
            b.disk_alerted = sorted(st.disks) if st.disks is not None else None
        await db.flush()
    sent = 0
    for user_id, text in dict.fromkeys(sends):  # one copy per person, in order
        await notify.dm(db, await db.get(User, user_id), text, now)
        sent += 1
    return sent
