from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.domain.rules import Rules
from app.domain.share import is_stale
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


def evaluate_idle(
    state: IdleState,
    latest: ServerUsage | None,
    now: datetime,
    resource: str,
    rules: Rules,
) -> IdleDecision:
    if latest is None or is_stale(latest.ts, now, rules):
        return IdleDecision(
            IdleAction.NONE, IdleState(state.warnings, None, None), None
        )
    if not _is_quiet(state, latest, resource, rules):
        return IdleDecision(IdleAction.NONE, IdleState(0, None, None), None)
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
