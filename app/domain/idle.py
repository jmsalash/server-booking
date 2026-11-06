from collections.abc import Sequence
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


def machine_resource(resources: Sequence[str]) -> str:
    """What to check on a machine: GPU quietness counts if any booking on it uses the GPU."""
    return "cpu_gpu" if "cpu_gpu" in resources else "cpu"


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
    resource: str,
    rules: Rules,
) -> list[IdleDecision]:
    """Idle step for every booking running on one machine (one, or two when shared).

    Quietness is a property of the machine: activity seen against any booking's RAM baseline
    resets them all. While quiet, each booking advances on its own idle clock and warning
    count, so an "I'm still using it" on one booking protects only that one.
    """
    missing = (
        latest is None
        or is_stale(latest.ts, now, rules)
        or (resource == "cpu_gpu" and not latest.gpus)
    )
    if missing:
        return [IdleDecision(IdleAction.NONE, IdleState(s.warnings, None, None), None)
                for s in states]
    assert latest is not None
    if not all(_is_quiet(s, latest, resource, rules) for s in states):
        return [IdleDecision(IdleAction.NONE, IdleState(0, None, None), None) for _ in states]
    return [_advance(s, latest, now, rules) for s in states]


def evaluate_idle(
    state: IdleState,
    latest: ServerUsage | None,
    now: datetime,
    resource: str,
    rules: Rules,
) -> IdleDecision:
    return evaluate_machine_idle([state], latest, now, resource, rules)[0]
