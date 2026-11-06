"""Judging metric readings: staleness, and RAM/disk "almost full" alerts."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.rules import Rules
from app.domain.types import DiskUsage, ServerUsage

# After an alert, a value must drop this many percentage points below the threshold before
# crossing it again alerts again, so a value hovering at the threshold does not spam.
REARM_PP = 5.0


def is_stale(latest_ts: datetime | None, now: datetime, rules: Rules) -> bool:
    if latest_ts is None:
        return True
    return now - latest_ts > timedelta(minutes=rules.stale_after_min)


@dataclass(frozen=True)
class FullState:
    """What a booking was already warned about: RAM, and the disk mounts."""
    ram: bool = False
    disks: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FullDecision:
    state: FullState
    ram_pct: float | None = None             # RAM to warn about now
    disks: tuple[DiskUsage, ...] = ()        # mounts to warn about now, worst first


def evaluate_full(
    state: FullState, latest: ServerUsage | None, now: datetime, rules: Rules
) -> FullDecision:
    """Warn once when RAM or a disk crosses its threshold; re-arm once it drops REARM_PP below.

    Stale or missing data neither warns nor re-arms; neither does a mount missing from the
    reading (e.g. the disk query failed).
    """
    if latest is None or is_stale(latest.ts, now, rules):
        return FullDecision(state)
    ram_alert = None
    ram = state.ram
    if ram:
        ram = latest.ram_pct >= rules.ram_alert_pct - REARM_PP
    elif latest.ram_pct >= rules.ram_alert_pct:
        ram, ram_alert = True, latest.ram_pct
    alerted = set(state.disks)
    new: list[DiskUsage] = []
    for d in latest.disks:
        if d.mount in alerted:
            if d.used_pct < rules.disk_alert_pct - REARM_PP:
                alerted.discard(d.mount)
        elif d.used_pct >= rules.disk_alert_pct:
            alerted.add(d.mount)
            new.append(d)
    new.sort(key=lambda d: (-d.used_pct, d.mount))
    return FullDecision(FullState(ram, frozenset(alerted)), ram_alert, tuple(new))
