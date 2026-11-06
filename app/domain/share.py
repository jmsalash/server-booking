from collections.abc import Sequence
from datetime import datetime, timedelta

from app.domain.rules import Rules
from app.domain.types import ServerUsage


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


def _high(win: Sequence[ServerUsage], gpu_count: int, rules: Rules) -> bool:
    """Mean CPU, RAM or any GPU's memory at or above the threshold over a non-empty window."""
    thr = rules.spike_threshold_pct
    if _mean([s.cpu_pct for s in win]) >= thr or _mean([s.ram_pct for s in win]) >= thr:
        return True
    return any(_mean([s.gpus[i].mem_pct for s in win]) >= thr for i in range(gpu_count))


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
    return _high(win, gpu_count, rules)


def spike_decision(already_alerted: bool, over: bool) -> tuple[bool, bool]:
    return (over and not already_alerted, over)
