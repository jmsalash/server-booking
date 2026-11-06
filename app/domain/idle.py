from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.domain.rules import Rules
from app.domain.readings import is_stale
from app.domain.status import format_hm_or_day
from app.domain.timeutil import (
    add_working_minutes, duration_text, in_working_hours, next_work_start, working_minutes,
)
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


def _is_quiet(state: IdleState, latest: ServerUsage, rules: Rules) -> bool:
    """Quiet means the whole machine is quiet: any CPU or GPU activity counts as use,
    whatever the booking booked (a CPU-only booking running GPU jobs is still working)."""
    if not latest.cpu_pct < rules.idle_cpu_pct:
        return False
    if not all(g.util_pct < rules.idle_gpu_util_pct for g in latest.gpus):
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
    rules: Rules,
) -> list[IdleDecision]:
    """Idle step for every booking running on one machine (one, or two when shared).

    Quietness is the machine's (CPU and every GPU low, RAM steady against each booking's own
    baseline). Each booking keeps its own idle clock and warning count, so an "I'm still
    using it" protects only that booking.
    """
    return [_evaluate_one(s, latest, now, rules) for s in states]


def _evaluate_one(
    state: IdleState, latest: ServerUsage | None, now: datetime, rules: Rules
) -> IdleDecision:
    missing = latest is None or is_stale(latest.ts, now, rules)
    if missing:
        return IdleDecision(IdleAction.NONE, IdleState(state.warnings, None, None), None)
    assert latest is not None
    if not _is_quiet(state, latest, rules):
        return IdleDecision(IdleAction.NONE, IdleState(0, None, None), None)
    return _advance(state, latest, now, rules)


def evaluate_idle(
    state: IdleState,
    latest: ServerUsage | None,
    now: datetime,
    rules: Rules,
) -> IdleDecision:
    return _evaluate_one(state, latest, now, rules)


QUIET_MAX_GAP = timedelta(minutes=10)  # about twice the metrics poll interval


def _snapshot_quiet(s: ServerUsage, baseline_ram: float, has_gpu: bool, rules: Rules) -> bool:
    """`_is_quiet`'s thresholds, for one snapshot of the whole machine."""
    if not s.cpu_pct < rules.idle_cpu_pct:
        return False
    if has_gpu and not s.gpus:  # GPU readings missing: can't call it quiet
        return False
    if not all(g.util_pct < rules.idle_gpu_util_pct for g in s.gpus):
        return False
    return abs(s.ram_pct - baseline_ram) < rules.idle_ram_delta_pp


def quiet_since(
    snapshots: Sequence[ServerUsage], now: datetime, rules: Rules, has_gpu: bool,
    not_before: datetime | None = None,
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
        if not_before is not None and s.ts < not_before:  # quiet before the booking began
            break
        if not _snapshot_quiet(s, baseline, has_gpu, rules):
            break
        since = prev_ts = s.ts
    return since


# --- "where do I stand" panel for My bookings --------------------------------------------------

POLL = timedelta(minutes=5)  # a quiet run shorter than one poll is not shown


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


@dataclass(frozen=True)
class IdlePanel:
    activity: str              # "Quiet for 1 h 20 min" / "Active" / "Usage unknown"
    quiet: bool
    warnings: str              # "1 of 3"
    explain: str               # "Released after the 3rd warning."
    next_step: str | None      # when the next warning (or the release) comes, if it stays quiet
    danger: bool               # one warning (or none) left
    offer_still_using: bool


def next_idle_step_at(idle_since: datetime | None, now: datetime, rules: Rules) -> datetime | None:
    """When idle tracking next acts (warns or releases) if the machine stays quiet: once the
    idle window has passed in working hours since `idle_since` (or since now, before the
    tracker has picked the quiet stretch up). Never earlier than now."""
    at = add_working_minutes(idle_since or now, rules.idle_window_min, rules)
    if at is None:
        return None
    return at if at > now else next_work_start(now, rules)


def idle_panel(
    snapshots: Sequence[ServerUsage], now: datetime, rules: Rules, has_gpu: bool,
    warnings: int, idle_since: datetime | None, booking_start: datetime | None = None,
) -> IdlePanel:
    """What My bookings shows for one running booking on a monitored machine.

    Quiet means the whole machine is quiet (CPU and GPUs), as for the idle warnings; the
    quiet time is wall-clock, the next warning counts working hours.
    """
    latest = max(snapshots, key=lambda s: s.ts, default=None)
    unknown = (latest is None or is_stale(latest.ts, now, rules)
               or (has_gpu and not latest.gpus))
    # Quiet time counts from the booking's start at the earliest, never from before it.
    since = None if unknown else quiet_since(snapshots, now, rules, has_gpu, booking_start)
    quiet = since is not None and now - since >= POLL
    if unknown:
        activity = "Usage unknown"
    elif quiet:
        activity = f"Quiet for {duration_text(int((now - since).total_seconds() // 60))}"
    else:
        activity = "Active"
    n = rules.max_idle_warnings
    next_step = None
    if quiet:
        at = next_idle_step_at(idle_since, now, rules)
        if at is not None:
            when = format_hm_or_day(at, rules.timezone, now)
            if warnings >= n:
                next_step = f"Will be released at ~{when} if it stays quiet"
            elif not in_working_hours(now, rules) and at == next_work_start(now, rules):
                next_step = f"Next warning when working hours resume ({when})"
            else:
                next_step = f"Next warning at ~{when} if it stays quiet"
    return IdlePanel(
        activity=activity, quiet=quiet, warnings=f"{warnings} of {n}",
        explain=f"Released after the {_ordinal(n)} warning.", next_step=next_step,
        danger=warnings > 0 and warnings >= n - 1,
        offer_still_using=warnings > 0 or quiet,
    )
