from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.domain.idle import (
    IdleAction, IdleState, evaluate_idle, evaluate_machine_idle, machine_resource,
)
from app.domain.rules import Rules
from app.domain.timeutil import parse_local
from app.domain.types import GpuUsage, ServerUsage

RULES = Rules()
TZ = RULES.timezone
NONE, WARN, RECLAIM = IdleAction.NONE, IdleAction.WARN, IdleAction.RECLAIM


def L(s):
    return parse_local(s, TZ)


def local(dt):
    return dt.astimezone(ZoneInfo(TZ)).strftime("%a %Y-%m-%dT%H:%M")


def quiet(now, i):
    return ServerUsage(now, 2.0, 30.0, (GpuUsage(1.0, 5.0),))


def drive(start, minutes, sample, resource="cpu_gpu", state=None):
    """Return [(now, decision)] stepping every 5 min; sample(now, i) may return None."""
    state = state or IdleState(0, None, None)
    out = []
    for i in range(minutes // 5 + 1):
        now = start + timedelta(minutes=5 * i)
        dec = evaluate_idle(state, sample(now, i), now, resource, RULES)
        state = dec.state
        out.append((now, dec))
        if dec.action == RECLAIM:  # caller ends the booking
            break
    return out


def events(results):
    return [(local(n), d.action) for n, d in results if d.action != NONE]


def test_two_quiet_working_hours_warns_once():
    res = drive(L("2026-11-10T09:00"), 125, quiet)
    assert events(res) == [("Tue 2026-11-10T11:00", WARN)]
    assert [d.warning_number for n, d in res if d.action == WARN] == [1]
    assert all(d.warning_number is None for n, d in res if d.action != WARN)


def test_three_warnings_then_reclaim():
    res = drive(L("2026-11-10T09:00"), 485, quiet)
    assert events(res) == [
        ("Tue 2026-11-10T11:00", WARN),
        ("Tue 2026-11-10T13:00", WARN),
        ("Tue 2026-11-10T15:00", WARN),
        ("Tue 2026-11-10T17:00", RECLAIM),
    ]
    assert [d.warning_number for n, d in res if d.action == WARN] == [1, 2, 3]


def test_activity_resets_warnings():
    def sample(now, i):
        if local(now) == "Tue 2026-11-10T12:00":
            return ServerUsage(now, 80.0, 30.0, (GpuUsage(1.0, 5.0),))
        return quiet(now, i)

    res = drive(L("2026-11-10T09:00"), 305, sample)
    # WARN 11:00 (#1); busy at 12:00 wipes warnings; window restarts at 12:05
    assert events(res) == [
        ("Tue 2026-11-10T11:00", WARN),
        ("Tue 2026-11-10T14:05", WARN),
    ]
    assert [d.warning_number for n, d in res if d.action == WARN] == [1, 1]
    busy = next(d for n, d in res if local(n) == "Tue 2026-11-10T12:00")
    assert busy.state == IdleState(0, None, None)


def test_night_does_not_count():
    res = drive(L("2026-11-10T18:00"), 16 * 60 + 5, quiet)
    assert events(res) == [("Wed 2026-11-11T10:00", WARN)]


def test_missing_data_resets_window_not_warnings():
    state = IdleState(2, L("2026-11-10T09:00"), 30.0)
    dec = evaluate_idle(state, None, L("2026-11-10T10:00"), "cpu_gpu", RULES)
    assert dec.action == NONE
    assert dec.state == IdleState(2, None, None)
    assert dec.warning_number is None
    # window restarts: quiet again needs a fresh 120 working minutes
    res = drive(L("2026-11-10T10:05"), 125, quiet, state=dec.state)
    assert events(res) == [("Tue 2026-11-10T12:05", WARN)]
    assert res[-1][1].state.warnings == 3


def test_stale_snapshot_never_reclaims():
    state = IdleState(3, L("2026-11-10T09:00"), 30.0)
    now = L("2026-11-10T17:00")
    stale = ServerUsage(now - timedelta(minutes=20), 2.0, 30.0, ())
    dec = evaluate_idle(state, stale, now, "cpu_gpu", RULES)
    assert dec.action == NONE
    assert dec.state == IdleState(3, None, None)


def test_cpu_booking_ignores_gpu_util():
    busy_gpu = lambda now, i: ServerUsage(now, 2.0, 30.0, (GpuUsage(90.0, 50.0),))
    res = drive(L("2026-11-10T09:00"), 125, busy_gpu, resource="cpu")
    assert events(res) == [("Tue 2026-11-10T11:00", WARN)]


def test_gpu_booking_busy_gpu_not_quiet():
    busy_gpu = lambda now, i: ServerUsage(now, 2.0, 30.0, (GpuUsage(1.0, 5.0), GpuUsage(90.0, 50.0)))
    res = drive(L("2026-11-10T09:00"), 245, busy_gpu, resource="cpu_gpu")
    assert events(res) == []
    assert res[-1][1].state == IdleState(0, None, None)


def test_ram_growth_breaks_quiet():
    def sample(now, i):
        ram = 30.0 if i < 12 else 33.0  # +3pp at 10:00
        return ServerUsage(now, 2.0, ram, (GpuUsage(1.0, 5.0),))

    res = drive(L("2026-11-10T09:00"), 125, sample)
    assert events(res) == []
    assert res[12][1].state == IdleState(0, None, None)


def test_window_across_dst_end():
    start = L("2026-10-23T18:00")
    end = L("2026-10-26T10:00")
    minutes = int((end - start).total_seconds() // 60) + 5
    res = drive(start, minutes, quiet)
    assert events(res) == [("Mon 2026-10-26T10:00", WARN)]


def test_cpu_gpu_booking_with_no_gpu_data_is_missing_data():
    no_gpu = lambda now, i: ServerUsage(now, 2.0, 30.0, ())
    state = IdleState(2, None, None)
    res = drive(L("2026-11-10T09:00"), 600, no_gpu, resource="cpu_gpu", state=state)
    assert events(res) == []
    assert all(d.state == IdleState(2, None, None) for n, d in res)


def drive_pair(start, minutes, sample, resource="cpu_gpu", ack=None):
    """Two bookings on one machine; `ack(now)` returns the index whose owner says still using."""
    states = [IdleState(0, None, None), IdleState(0, None, None)]
    out = []
    for i in range(minutes // 5 + 1):
        now = start + timedelta(minutes=5 * i)
        if ack and (k := ack(now)) is not None:
            states[k] = IdleState(0, None, None)
        decs = evaluate_machine_idle(states, sample(now, i), now, resource, RULES)
        states = [d.state for d in decs]
        out.append((now, decs))
    return out


def pair_events(results, k):
    return [(local(n), ds[k].action) for n, ds in results if ds[k].action != NONE]


def test_shared_machine_warns_both():
    res = drive_pair(L("2026-11-10T09:00"), 125, quiet)
    assert pair_events(res, 0) == pair_events(res, 1) == [("Tue 2026-11-10T11:00", WARN)]


def test_shared_still_using_protects_only_that_booking():
    ack = lambda now: 1 if local(now) in ("Tue 2026-11-10T12:00", "Tue 2026-11-10T14:00",
                                          "Tue 2026-11-10T16:00") else None
    res = drive_pair(L("2026-11-10T09:00"), 485, quiet, ack=ack)
    first = next(e for e in pair_events(res, 0) if e[1] == RECLAIM)
    assert first == ("Tue 2026-11-10T17:00", RECLAIM)
    assert RECLAIM not in [a for _, a in pair_events(res, 1)]


def test_shared_activity_resets_both():
    def sample(now, i):
        if local(now) == "Tue 2026-11-10T12:00":
            return ServerUsage(now, 80.0, 30.0, (GpuUsage(1.0, 5.0),))
        return quiet(now, i)

    res = drive_pair(L("2026-11-10T09:00"), 125 + 60, sample)
    busy = next(ds for n, ds in res if local(n) == "Tue 2026-11-10T12:00")
    assert [d.state for d in busy] == [IdleState(0, None, None)] * 2


def test_shared_ram_change_against_one_baseline_resets_both():
    t = L("2026-11-10T10:00")
    states = [IdleState(1, L("2026-11-10T09:00"), 30.0), IdleState(0, None, None)]
    decs = evaluate_machine_idle(states, ServerUsage(t, 2.0, 35.0, (GpuUsage(1.0, 5.0),)),
                                 t, "cpu", RULES)
    assert [d.state for d in decs] == [IdleState(0, None, None)] * 2


def test_machine_resource():
    assert machine_resource(["cpu", "cpu"]) == "cpu"
    assert machine_resource(["cpu", "cpu_gpu"]) == "cpu_gpu"
