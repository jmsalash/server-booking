from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.domain.rules import Rules
from app.domain.readings import is_stale
from app.domain.timeutil import in_working_hours, working_minutes
from app.domain.types import ServerUsage


@dataclass(frozen=True)
class IdleState:
    warnings: int
    idle_since: datetime | None
    ram_baseline: float | None


class IdleAction(StrEnum):
    NONE = "none"
    WARN = "warn"
    RECLAIM = "reclaim"


@dataclass(frozen=True)
class IdleDecision:
    action: IdleAction
    state: IdleState
    warning_number: int | None


def _is_quiet(
    state: IdleState, latest: ServerUsage, resource: str, rules: Rules
) -> bool:
    if not latest.cpu_pct < rules.idle_cpu_pct:
        return False
    if resource == "cpu_gpu" and not all(
        g.util_pct < rules.idle_gpu_util_pct for g in latest.gpus
    ):
        return False
    if state.ram_baseline is not None and not (
        abs(latest.ram_pct - state.ram_baseline) < rules.idle_ram_delta_pp
    ):
        return False
    return True


def _advance(state: IdleState, latest: ServerUsage, now: datetime, rules: Rules) -> IdleDecision:
    """One booking's step once the machine is known to be quiet."""
    if state.idle_since is None:
        return IdleDecision(
            IdleAction.NONE, IdleState(state.warnings, latest.ts, latest.ram_pct), None
        )
    if (
        not in_working_hours(now, rules)
        or working_minutes(state.idle_since, now, rules) < rules.idle_window_min
    ):
        return IdleDecision(IdleAction.NONE, state, None)
    if state.warnings < rules.max_idle_warnings:
        n = state.warnings + 1
        return IdleDecision(IdleAction.WARN, IdleState(n, now, latest.ram_pct), n)
    return IdleDecision(IdleAction.RECLAIM, state, None)


def evaluate_machine_idle(
    states: Sequence[IdleState],
    latest: ServerUsage | None,
    now: datetime,
    resources: Sequence[str],
    rules: Rules,
) -> list[IdleDecision]:
    """Idle step for every booking running on one machine (one, or two when shared).

    Each booking is judged on what it booked: a "cpu" booking only on CPU and RAM (GPU
    activity is ignored), a "cpu_gpu" booking also on GPU utilisation. Each booking keeps its
    own idle clock and warning count, so an "I'm still using it" protects only that booking.
    """
    return [_evaluate_one(s, latest, now, r, rules) for s, r in zip(states, resources, strict=True)]


def _evaluate_one(
    state: IdleState, latest: ServerUsage | None, now: datetime, resource: str, rules: Rules
) -> IdleDecision:
    missing = (
        latest is None
        or is_stale(latest.ts, now, rules)
        or (resource == "cpu_gpu" and not latest.gpus)
    )
    if missing:
        return IdleDecision(IdleAction.NONE, IdleState(state.warnings, None, None), None)
    assert latest is not None
    if not _is_quiet(state, latest, resource, rules):
        return IdleDecision(IdleAction.NONE, IdleState(0, None, None), None)
    return _advance(state, latest, now, rules)


def evaluate_idle(
    state: IdleState,
    latest: ServerUsage | None,
    now: datetime,
    resource: str,
    rules: Rules,
) -> IdleDecision:
    return _evaluate_one(state, latest, now, resource, rules)


QUIET_MAX_GAP = timedelta(minutes=10)  # about twice the metrics poll interval


def _snapshot_quiet(s: ServerUsage, baseline_ram: float, has_gpu: bool, rules: Rules) -> bool:
    """`_is_quiet`'s thresholds, for one snapshot of the whole machine."""
    if not s.cpu_pct < rules.idle_cpu_pct:
        return False
    if has_gpu and (not s.gpus or not all(g.util_pct < rules.idle_gpu_util_pct for g in s.gpus)):
        return False
    return abs(s.ram_pct - baseline_ram) < rules.idle_ram_delta_pp


def quiet_since(
    snapshots: Sequence[ServerUsage], now: datetime, rules: Rules, has_gpu: bool
) -> datetime | None:
    """Start of the machine's current quiet stretch, from its metric snapshots.

    Walks back from the latest snapshot while snapshots stay quiet (CPU low, every GPU idle
    when the server has GPUs, RAM within the idle delta of the latest reading) and no more
    than QUIET_MAX_GAP apart. None when the latest snapshot is missing, stale or active.
    Informational (bot /status); unlike the idle tracking it ignores working hours.
    """
    ordered = sorted(snapshots, key=lambda s: s.ts, reverse=True)
    if not ordered or is_stale(ordered[0].ts, now, rules):
        return None
    baseline = ordered[0].ram_pct
    since = None
    prev_ts = None
    for s in ordered:
        if prev_ts is not None and prev_ts - s.ts > QUIET_MAX_GAP:
            break
        if not _snapshot_quiet(s, baseline, has_gpu, rules):
            break
        since = prev_ts = s.ts
    return since
