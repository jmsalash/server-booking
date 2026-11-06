from datetime import timedelta

from app.domain.idle import idle_panel, next_idle_step_at
from app.domain.rules import Rules
from app.domain.timeutil import add_working_minutes, next_work_start, parse_local
from app.domain.types import GpuUsage, ServerUsage

R = Rules()  # Mon-Fri 09:00-19:00 Europe/Rome, 120 min window, 3 warnings


def L(v):
    return parse_local(v, R.timezone)


def snaps(now, minutes, cpu=1.0, gpu_util=0.0, active_before=True):
    """Quiet readings every 5 min for `minutes` up to now (an active one before, if asked)."""
    out = [ServerUsage(now - timedelta(minutes=m), cpu, 30.0, (GpuUsage(gpu_util, 1.0),))
           for m in range(0, minutes + 1, 5)]
    if active_before:
        out.append(ServerUsage(now - timedelta(minutes=minutes + 5), 90.0, 30.0,
                               (GpuUsage(90.0, 1.0),)))
    return out


def test_add_working_minutes_skips_nights_and_weekends():
    assert add_working_minutes(L("2026-11-10T14:00"), 120, R) == L("2026-11-10T16:00")
    assert add_working_minutes(L("2026-11-10T18:00"), 120, R) == L("2026-11-11T10:00")
    assert add_working_minutes(L("2026-11-13T22:00"), 30, R) == L("2026-11-16T09:30")  # Fri→Mon
    assert next_work_start(L("2026-11-14T12:00"), R) == L("2026-11-16T09:00")
    assert next_work_start(L("2026-11-10T12:00"), R) == L("2026-11-10T12:00")


def test_quiet_panel_shows_next_warning_time():
    now = L("2026-11-10T14:20")
    p = idle_panel(snaps(now, 80), now, R, True, 1, L("2026-11-10T13:40"))
    assert p.activity == "Quiet for 1 h 20 min" and p.quiet
    assert p.warnings == "1 of 3" and p.explain == "Released after the 3rd warning."
    assert p.next_step == "Next warning at ~15:40 if it stays quiet"
    assert p.offer_still_using and not p.danger


def test_last_step_says_released():
    now = L("2026-11-10T14:20")
    p = idle_panel(snaps(now, 30), now, R, True, 3, L("2026-11-10T13:40"))
    assert p.next_step == "Will be released at ~15:40 if it stays quiet" and p.danger


def test_danger_from_second_warning():
    now = L("2026-11-10T14:20")
    assert idle_panel(snaps(now, 0, cpu=80.0), now, R, False, 2, None).danger
    assert not idle_panel(snaps(now, 0, cpu=80.0), now, R, False, 0, None).danger


def test_outside_working_hours_waits_for_them():
    now = L("2026-11-14T11:00")  # Saturday
    # The window already passed during Friday: the warning comes as working hours resume.
    p = idle_panel(snaps(now, 60), now, R, False, 1, L("2026-11-13T15:00"))
    assert p.next_step == "Next warning when working hours resume (Mon 16 09:00)"
    # Only 30 working minutes so far: 90 more after Monday 09:00.
    p = idle_panel(snaps(now, 60), now, R, False, 1, L("2026-11-13T18:30"))
    assert p.next_step == "Next warning at ~Mon 16 10:30 if it stays quiet"


def test_not_yet_tracked_counts_from_now():
    now = L("2026-11-10T14:20")
    assert next_idle_step_at(None, now, R) == L("2026-11-10T16:20")


def test_active_unknown_and_short_quiet():
    now = L("2026-11-10T14:20")
    p = idle_panel(snaps(now, 0, cpu=80.0), now, R, False, 0, None)
    assert (p.activity, p.next_step, p.offer_still_using) == ("Active", None, False)
    # A single quiet reading (less than a poll) is not shown as quiet.
    assert idle_panel(snaps(now, 0), now, R, False, 0, None).activity == "Active"
    stale = snaps(now - timedelta(minutes=30), 60)
    assert idle_panel(stale, now, R, False, 0, None).activity == "Usage unknown"
    assert idle_panel([], now, R, False, 0, None).activity == "Usage unknown"


def test_cpu_only_booking_ignores_gpu():
    now = L("2026-11-10T14:20")
    busy_gpu = snaps(now, 40, gpu_util=95.0)
    assert idle_panel(busy_gpu, now, R, False, 0, None).activity == "Quiet for 40 min"
    assert idle_panel(busy_gpu, now, R, True, 0, None).activity == "Active"
