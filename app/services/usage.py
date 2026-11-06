from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.types import DiskUsage, GpuUsage, ServerUsage
from app.models import MetricSnapshot


def _disks(raw) -> tuple[DiskUsage, ...]:
    """Disks from a snapshot row; rows from before disks were stored have none."""
    out = []
    for d in raw if isinstance(raw, list) else []:
        try:
            out.append(DiskUsage(str(d["mount"]), float(d["used_pct"])))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(out)


def _to_usage(row: MetricSnapshot) -> ServerUsage:
    return ServerUsage(
        ts=row.ts,
        cpu_pct=row.cpu_pct,
        ram_pct=row.ram_pct,
        gpus=tuple(GpuUsage(g["util_pct"], g["mem_pct"], g.get("index"), g.get("model"))
                   for g in (row.gpus or [])),
        disks=_disks(row.disks),
    )


async def save_snapshot(db: AsyncSession, server_id: int, usage: ServerUsage) -> None:
    db.add(MetricSnapshot(
        server_id=server_id, ts=usage.ts, cpu_pct=usage.cpu_pct, ram_pct=usage.ram_pct,
        gpus=[{"util_pct": g.util_pct, "mem_pct": g.mem_pct, "index": g.index, "model": g.model}
              for g in usage.gpus],
        disks=[{"mount": d.mount, "used_pct": d.used_pct} for d in usage.disks],
    ))
    await db.flush()


async def recent(db: AsyncSession, server_id: int, since: datetime) -> list[ServerUsage]:
    rows = await db.scalars(
        select(MetricSnapshot)
        .where(MetricSnapshot.server_id == server_id, MetricSnapshot.ts >= since)
        .order_by(MetricSnapshot.ts, MetricSnapshot.id)
    )
    return [_to_usage(r) for r in rows]


async def latest(db: AsyncSession, server_id: int) -> ServerUsage | None:
    row = await db.scalar(
        select(MetricSnapshot).where(MetricSnapshot.server_id == server_id)
        .order_by(MetricSnapshot.ts.desc(), MetricSnapshot.id.desc()).limit(1)
    )
    return _to_usage(row) if row else None


async def prune(db: AsyncSession, before: datetime) -> int:
    res = await db.execute(delete(MetricSnapshot).where(MetricSnapshot.ts < before))
    await db.flush()
    return res.rowcount
