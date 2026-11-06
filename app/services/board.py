from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import share
from app.domain.idle import quiet_since
from app.domain.rules import Rules
from app.domain.status import compute_status
from app.domain.types import BookingView, ServerInfo, ServerStatus, ServerUsage
from app.models import Booking, Server, User
from app.services import usage
from app.services.inventory import get_live, list_servers, to_info
from app.settings_store import SettingsStore

HEALTH_KEY = "health.last_poll"  # last worker poll, written by app.jobs.metrics
QUIET_LOOKBACK = timedelta(hours=12)  # bounds the snapshot query for quiet_since

UPCOMING_WINDOW = timedelta(days=14)


@dataclass(frozen=True)
class ServerCard:
    server: ServerInfo
    status: ServerStatus
    usage: ServerUsage | None
    stale: bool
    upcoming: list[BookingView]
    # Monitored but no reading saved yet and not failing either: a newly added
    # server waiting for the worker's first poll. Not counted as monitoring down.
    pending: bool = False
    # Bookings running now, the primary first and the partner (if shared) second.
    current: tuple[BookingView, ...] = ()
    # Start of the first booking that starts after now.
    next_start: datetime | None = None
    # In use but quiet for at least the idle window: since when (bot /status only).
    quiet_since: datetime | None = None


def to_view(b: Booking, user_name: str) -> BookingView:
    return BookingView(
        id=b.id, user_id=b.user_id, user_name=user_name, server_id=b.server_id,
        start=b.start_at, end=b.end_at, resource=b.resource, is_shared=b.is_shared,
        idle_warnings=b.idle_warnings or 0, shareable=b.shareable,
    )


async def active_bookings(db: AsyncSession, server_id: int, now: datetime) -> list[BookingView]:
    """Active bookings on a server that have not ended yet, sorted by start."""
    rows = await db.execute(
        select(Booking, User.display_name)
        .join(User, User.id == Booking.user_id)
        .where(Booking.server_id == server_id, Booking.status == "active", Booking.end_at > now)
        .order_by(Booking.start_at, Booking.id)
    )
    return [to_view(b, name) for b, name in rows.all()]


def overlapping(b: BookingView, others: Sequence[BookingView]) -> list[BookingView]:
    """The other bookings that share time with `b` (its partners, when shared)."""
    return [x for x in others if x.id != b.id and x.start < b.end and b.start < x.end]


async def _failed_last_poll(store: SettingsStore) -> set[str]:
    """Names of servers whose metrics failed in the worker's last poll."""
    last = await store.get(HEALTH_KEY) or {}
    return set((last.get("failed") or {}).keys())


async def _card(
    db: AsyncSession, server: Server, now: datetime, rules: Rules, failed: set[str],
    with_quiet: bool = False,
) -> ServerCard:
    info = to_info(server)
    bookings = await active_bookings(db, server.id, now)
    status = compute_status(info, bookings, now, rules)
    if server.kind == "grafana":
        latest = await usage.latest(db, server.id)
        stale = share.is_stale(latest.ts if latest else None, now, rules)
        pending = latest is None and server.name not in failed
    else:
        latest, stale, pending = None, False, False
    horizon = now + UPCOMING_WINDOW
    upcoming = [b for b in bookings if b.start < horizon]
    current = tuple(sorted((b for b in bookings if b.start <= now < b.end),
                           key=lambda b: (b.is_shared, b.start, b.id)))
    next_start = next((b.start for b in bookings if b.start > now), None)
    quiet = None
    if with_quiet and current and server.kind == "grafana" and not stale:
        snaps = await usage.recent(db, server.id, now - QUIET_LOOKBACK)
        since = quiet_since(snaps, now, rules, info.has_gpu)
        if since is not None and now - since >= timedelta(minutes=rules.idle_window_min):
            quiet = since
    return ServerCard(info, status, latest, stale, upcoming, pending, current, next_start,
                      quiet)


async def build_card(
    db: AsyncSession, store: SettingsStore, server_id: int, now: datetime,
    with_quiet: bool = False,
) -> ServerCard:
    """`with_quiet` also works out how long a machine in use has been quiet (bot only)."""
    server = await get_live(db, server_id)
    return await _card(db, server, now, await store.get_rules(), await _failed_last_poll(store),
                       with_quiet)


async def build_board(
    db: AsyncSession, store: SettingsStore, now: datetime, with_quiet: bool = False
) -> list[ServerCard]:
    rules = await store.get_rules()
    failed = await _failed_last_poll(store)
    return [await _card(db, s, now, rules, failed, with_quiet)
            for s in await list_servers(db, include_disabled=True)]


def monitoring_down(cards: Sequence[ServerCard]) -> bool:
    """True when every enabled Grafana-monitored server has stale metrics.

    Servers still waiting for their first reading are left out, so adding
    servers does not raise the alarm before the worker has polled them.
    """
    monitored = [c for c in cards
                 if c.server.kind == "grafana" and c.server.enabled and not c.pending]
    return bool(monitored) and all(c.stale for c in monitored)
