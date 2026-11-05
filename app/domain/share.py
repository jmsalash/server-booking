from collections.abc import Sequence
from datetime import datetime, timedelta

from app.domain.rules import Rules
from app.domain.types import ServerInfo, ServerUsage


def is_stale(latest_ts: datetime | None, now: datetime, rules: Rules) -> bool:
    if latest_ts is None:
        return True
    return now - latest_ts > timedelta(minutes=rules.stale_after_min)


def _window(
    snapshots: Sequence[ServerUsage], now: datetime, minutes: int
) -> list[ServerUsage]:
    lo = now - timedelta(minutes=minutes)
    return [s for s in snapshots if lo < s.ts <= now]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _usage_ok(win: Sequence[ServerUsage], gpu_count: int, rules: Rules) -> bool:
    """CPU/RAM/GPU rule over non-empty window whose GPU counts already match."""
    thr = rules.share_threshold_pct
    if not (_mean([s.cpu_pct for s in win]) < thr):
        return False
    if not (_mean([s.ram_pct for s in win]) < thr):
        return False
    if gpu_count <= 0:
        return True
    avg_util = [_mean([s.gpus[i].util_pct for s in win]) for i in range(gpu_count)]
    avg_mem = [_mean([s.gpus[i].mem_pct for s in win]) for i in range(gpu_count)]
    if gpu_count == 1:
        return avg_mem[0] < thr
    idle = sum(
        1
        for u, m in zip(avg_util, avg_mem)
        if u < rules.gpu_idle_util_pct and m < rules.gpu_idle_mem_pct
    )
    return idle >= gpu_count / 2


def is_shareable_now(
    server: ServerInfo,
    active_now: int,
    snapshots: Sequence[ServerUsage],
    now: datetime,
    rules: Rules,
) -> bool:
    if not server.shareable or server.kind != "grafana" or active_now != 1:
        return False
    latest = max((s.ts for s in snapshots), default=None)
    if is_stale(latest, now, rules):
        return False
    win = _window(snapshots, now, rules.share_avg_min)
    if not win:
        return False
    gpu_count = server.gpu_count if server.has_gpu else 0
    if gpu_count and any(len(s.gpus) != gpu_count for s in win):
        return False
    return _usage_ok(win, gpu_count, rules)


def is_over_threshold(
    snapshots: Sequence[ServerUsage],
    now: datetime,
    rules: Rules,
    window_min: int,
    gpu_count: int,
) -> bool:
    win = [
        s
        for s in _window(snapshots, now, window_min)
        if not gpu_count or len(s.gpus) == gpu_count
    ]
    if not win:
        return False
    return not _usage_ok(win, gpu_count, rules)


def spike_decision(already_alerted: bool, over: bool) -> tuple[bool, bool]:
    return (over and not already_alerted, over)
