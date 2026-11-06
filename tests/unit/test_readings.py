from datetime import timedelta

from app.domain.readings import FullState, evaluate_full, is_stale
from app.domain.rules import Rules
from app.domain.timeutil import parse_local
from app.domain.types import DiskUsage, ServerUsage

R = Rules()
now = parse_local("2026-11-10T12:20", R.timezone)


def reading(ram=30.0, disks=(), age_min=0):
    return ServerUsage(now - timedelta(minutes=age_min), 5.0, ram, (),
                       tuple(DiskUsage(m, p) for m, p in disks))


def test_is_stale():
    assert is_stale(None, now, R)
    assert not is_stale(now - timedelta(minutes=15), now, R)
    assert is_stale(now - timedelta(minutes=16), now, R)


def test_rules_defaults_and_old_stored_keys_ignored():
    assert (R.ram_alert_pct, R.disk_alert_pct) == (90.0, 90.0)
    old = {**Rules().to_dict(), "share_threshold_pct": 40.0, "share_avg_min": 60,
           "spike_threshold_pct": 70.0, "spike_avg_min": 15, "gpu_idle_util_pct": 5.0}
    r = Rules.from_dict(old)
    assert r == Rules()
    assert not hasattr(r, "spike_threshold_pct") and not hasattr(r, "share_avg_min")


def test_ram_alerts_once_then_rearms_below_threshold_minus_5():
    s = FullState()
    d = evaluate_full(s, reading(ram=89.9), now, R)
    assert d.ram_pct is None and not d.state.ram
    d = evaluate_full(s, reading(ram=94.0), now, R)
    assert d.ram_pct == 94.0 and d.state.ram
    s = d.state
    for ram in (99.0, 90.0, 85.0):  # still high, or not yet 5 pp below: no repeat, stays armed off
        d = evaluate_full(s, reading(ram=ram), now, R)
        assert d.ram_pct is None and d.state.ram, ram
    d = evaluate_full(s, reading(ram=84.9), now, R)  # re-armed
    assert d.ram_pct is None and not d.state.ram
    d = evaluate_full(d.state, reading(ram=91.0), now, R)
    assert d.ram_pct == 91.0 and d.state.ram


def test_disk_alerts_per_mount_worst_first():
    d = evaluate_full(FullState(), reading(disks=[("/", 41.0), ("/data", 92.0),
                                                  ("/scratch", 96.0)]), now, R)
    assert [x.mount for x in d.disks] == ["/scratch", "/data"]
    assert d.state.disks == {"/data", "/scratch"} and d.ram_pct is None
    # /data drops below 85 and re-arms; /scratch stays alerted; / crosses for the first time.
    d = evaluate_full(d.state, reading(disks=[("/", 90.0), ("/data", 80.0),
                                              ("/scratch", 99.0)]), now, R)
    assert [x.mount for x in d.disks] == ["/"]
    assert d.state.disks == {"/", "/scratch"}


def test_stale_or_missing_data_neither_alerts_nor_rearms():
    alerted = FullState(True, frozenset({"/data"}))
    for latest in (None, reading(ram=10.0, disks=[("/data", 10.0)], age_min=16)):
        d = evaluate_full(alerted, latest, now, R)
        assert d.state == alerted and d.ram_pct is None and not d.disks
    d = evaluate_full(FullState(), reading(ram=99.0, disks=[("/", 99.0)], age_min=16), now, R)
    assert d.state == FullState() and d.ram_pct is None and not d.disks
    # A mount missing from the reading (disk query failed) stays alerted.
    d = evaluate_full(alerted, reading(ram=95.0), now, R)
    assert d.state == alerted


def test_thresholds_follow_rules():
    r = Rules(ram_alert_pct=50.0, disk_alert_pct=70.0)
    d = evaluate_full(FullState(), reading(ram=50.0, disks=[("/", 70.0)]), now, r)
    assert d.ram_pct == 50.0 and [x.mount for x in d.disks] == ["/"]
