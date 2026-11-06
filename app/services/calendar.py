"""The Calendar page: every live machine's bookings for one week."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.calendar import Bar, Week, layout_bars
from app.domain.types import GpuUsage, ServerInfo
from app.models import Booking, MetricSnapshot, Server, User
from app.services.board import to_view
from app.services.inventory import list_servers

SHOWN = ("active", "completed")  # not cancelled or reclaimed


@dataclass(frozen=True)
class CalendarRow:
    server: Server
    sub: str  # `4× RTX 4090`, `2 GPUs`, `CPU only`
    bars: list[Bar]


def machine_sub(server: Server | ServerInfo, gpus: list[dict] | None) -> str:
    """The board card's sub-line: GPU models from the last reading, else the count."""
    models = []
    for g in gpus or []:
        m = GpuUsage(0, 0, model=g.get("model")).short_model
        if m and m not in models:
            models.append(m)
    if models:
        return (f"{len(gpus)}× " if len(gpus) > 1 else "") + ", ".join(models)
    if server.has_gpu:
        return f"{server.gpu_count} GPU" + ("s" if server.gpu_count != 1 else "")
    return "CPU only"


async def _latest_gpus(db: AsyncSession) -> dict[int, list[dict]]:
    rows = await db.execute(
        select(MetricSnapshot.server_id, MetricSnapshot.gpus)
        .ext(distinct_on(MetricSnapshot.server_id))
        .order_by(MetricSnapshot.server_id, MetricSnapshot.ts.desc(), MetricSnapshot.id.desc())
    )
    return {sid: gpus for sid, gpus in rows.all()}


async def build_week(db: AsyncSession, week: Week, now: datetime) -> list[CalendarRow]:
    """One row per live server (disabled ones too), in name order."""
    servers = await list_servers(db, include_disabled=True)
    rows = await db.execute(
        select(Booking, User.display_name)
        .join(User, User.id == Booking.user_id)
        .join(Server, Server.id == Booking.server_id)
        .where(Server.archived_at.is_(None), Booking.status.in_(SHOWN),
               Booking.start_at < week.end, Booking.end_at > week.start)
        .order_by(Booking.server_id, Booking.start_at, Booking.id)
    )
    by_server = defaultdict(list)
    for b, name in rows.all():
        by_server[b.server_id].append(to_view(b, name))
    gpus = await _latest_gpus(db)
    return [CalendarRow(s, machine_sub(s, gpus.get(s.id)),
                        layout_bars(by_server[s.id], week, now))
            for s in servers]
