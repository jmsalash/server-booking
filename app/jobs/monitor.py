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


async def _active_now(db: AsyncSession, now: datetime) -> dict[int, list[Booking]]:
    """Active bookings running at `now` on enabled Grafana servers, grouped by server id."""
    rows = await db.scalars(
        select(Booking)
        .join(Server, Server.id == Booking.server_id)
        .where(
            Booking.status == "active", Booking.start_at <= now, Booking.end_at > now,
            Server.enabled.is_(True), Server.kind == "grafana",
        )
        .order_by(Booking.server_id, Booking.created_at, Booking.id)
        # Lock so a concurrent "still using it" acknowledgement is not overwritten.
        .with_for_update(of=Booking)
        .execution_options(populate_existing=True)
    )
    by_server: dict[int, list[Booking]] = defaultdict(list)
    for b in rows:
        by_server[b.server_id].append(b)
    return by_server


async def idle_check(
    db: AsyncSession, store: SettingsStore, now: datetime
) -> list[tuple[int, IdleAction]]:
    rules = await store.get_rules()
    actions: list[tuple[int, IdleAction]] = []
    for server_id, bookings in (await _active_now(db, now)).items():
        if len(bookings) != 1 or bookings[0].is_shared:
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


def _shared_of(pair: list[Booking]) -> Booking:
    flagged = [b for b in pair if b.is_shared]
    if len(flagged) == 1:
        return flagged[0]
    return pair[1]  # ordered by created_at, id: the later-created one


async def spike_check(db: AsyncSession, store: SettingsStore, now: datetime) -> int:
    rules = await store.get_rules()
    sent = 0
    for server_id, pair in (await _active_now(db, now)).items():
        if len(pair) != 2:
            continue
        server = await db.get(Server, server_id)
        gpu_count = server.gpu_count if server.has_gpu else 0
        snaps = await usage.recent(db, server_id, now - timedelta(minutes=rules.spike_avg_min))
        over = share.is_over_threshold(snaps, now, rules, rules.spike_avg_min, gpu_count)
        shared = _shared_of(pair)
        send, shared.spike_alerted = share.spike_decision(shared.spike_alerted, over)
        await db.flush()
        if not send:
            continue
        users = [await db.get(User, b.user_id) for b in pair]
        for me, other in ((users[0], users[1]), (users[1], users[0])):
            text = (
                f"⚠️ Usage on {server.name} is high. "
                f"You are sharing it with {other.display_name}."
            )
            await notify.dm(db, me, text, now)
        sent += 1
    return sent
