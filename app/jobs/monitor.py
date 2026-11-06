from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import format
from app.domain import share
from app.domain.idle import IdleAction, IdleState, evaluate_idle
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


def _reset_idle(b: Booking) -> None:
    b.idle_since = None
    b.idle_ram_baseline = None
    b.idle_warnings = 0


async def idle_check(
    db: AsyncSession, store: SettingsStore, now: datetime
) -> list[tuple[int, IdleAction]]:
    rules = await store.get_rules()
    actions: list[tuple[int, IdleAction]] = []
    for server_id, bookings in await _active_now(db, now, lock=True):
        if len(bookings) != 1 or bookings[0].is_shared:
            # Idle tracking is paused while shared; start afresh once the partner leaves
            # rather than resuming from an old idle_since / warning count.
            for b in bookings:
                _reset_idle(b)
            await db.flush()
            continue
        b = bookings[0]
        state = IdleState(b.idle_warnings, b.idle_since, b.idle_ram_baseline)
        latest = await usage.latest(db, server_id)
        decision = evaluate_idle(state, latest, now, b.resource, rules)
        b.idle_warnings = decision.state.warnings
        b.idle_since = decision.state.idle_since
        b.idle_ram_baseline = decision.state.ram_baseline
        if decision.action == IdleAction.WARN:
            b.last_warning_at = now
        await db.flush()
        if decision.action == IdleAction.WARN:
            server = await db.get(Server, server_id)
            owner = await db.get(User, b.user_id)
            text = format.idle_warning(
                server.name, decision.warning_number, rules.max_idle_warnings,
                rules.idle_window_min,
            )
            await notify.dm(db, owner, text, now, buttons=[("I'm still using it", f"still:{b.id}")])
        elif decision.action == IdleAction.RECLAIM:
            await booking_svc.reclaim(db, store, b.id, now)
        if decision.action != IdleAction.NONE:
            actions.append((b.id, decision.action))
    return actions


def _by_creation(pair: list[Booking]) -> list[Booking]:
    return sorted(pair, key=lambda b: (b.created_at, b.id))


def _shared_of(pair: list[Booking]) -> Booking:
    flagged = [b for b in pair if b.is_shared]
    if len(flagged) == 1:
        return flagged[0]
    return _by_creation(pair)[1]  # the later-created one


async def spike_check(db: AsyncSession, store: SettingsStore, now: datetime) -> int:
    rules = await store.get_rules()
    alerts: list[tuple[Server, list[Booking]]] = []
    flags: dict[int, bool] = {}
    # Read without locks; only the shared bookings whose flag changes are locked below.
    for server_id, pair in await _active_now(db, now, lock=False):
        if len(pair) != 2:
            continue
        server = await db.get(Server, server_id)
        gpu_count = server.gpu_count if server.has_gpu else 0
        snaps = await usage.recent(db, server_id, now - timedelta(minutes=rules.spike_avg_min))
        over = share.is_over_threshold(snaps, now, rules, rules.spike_avg_min, gpu_count)
        shared = _shared_of(pair)
        send, flag = share.spike_decision(shared.spike_alerted, over)
        if flag != shared.spike_alerted:
            flags[shared.id] = flag
        if send:
            alerts.append((server, _by_creation(pair)))
    if flags:
        locked = await db.scalars(
            select(Booking).where(Booking.id.in_(flags)).order_by(Booking.id)
            .with_for_update().execution_options(populate_existing=True)
        )
        for b in locked:
            b.spike_alerted = flags[b.id]
        await db.flush()
    for server, pair in alerts:
        users = [await db.get(User, b.user_id) for b in pair]
        for me, other in ((users[0], users[1]), (users[1], users[0])):
            text = (
                f"⚠️ Usage on {server.name} is high. "
                f"You are sharing it with {other.display_name}."
            )
            await notify.dm(db, me, text, now)
    return len(alerts)
