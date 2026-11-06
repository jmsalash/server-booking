from datetime import timedelta

from app.domain.rules import Rules
from app.domain.share import is_over_threshold, is_stale, spike_decision
from app.domain.timeutil import parse_local
from app.domain.types import GpuUsage, ServerUsage

R = Rules()
now = parse_local("2026-11-10T12:20", R.timezone)


def snaps(cpu, ram, gpus=(), minutes=60, end=None):
    end = end or now
    return [
        ServerUsage(
            end - timedelta(minutes=m),
            cpu,
            ram,
            tuple(GpuUsage(u, mm) for u, mm in gpus),
        )
        for m in range(minutes, -1, -5)
    ]


def test_is_stale():
    assert is_stale(None, now, R)
    assert not is_stale(now - timedelta(minutes=15), now, R)
    assert is_stale(now - timedelta(minutes=16), now, R)


def test_is_over_threshold():
    assert is_over_threshold(snaps(80, 10, minutes=15), now, R, 15, 0)
    assert not is_over_threshold(snaps(20, 10, minutes=15), now, R, 15, 0)
    assert not is_over_threshold([], now, R, 15, 0)
    assert is_over_threshold(snaps(10, 10, gpus=[(0, 80)], minutes=15), now, R, 15, 1)
    assert not is_over_threshold(snaps(10, 10, gpus=[(0, 20)], minutes=15), now, R, 15, 1)


def test_over_threshold_any_gpu_memory():
    assert is_over_threshold(snaps(10, 10, gpus=[(0, 5), (0, 60)], minutes=15), now, R, 15, 2)
    assert not is_over_threshold(snaps(10, 10, gpus=[(99, 5)] * 2, minutes=15), now, R, 15, 2)


def test_rules_load_old_stored_share_keys():
    old = {**Rules().to_dict(), "share_threshold_pct": 40.0, "share_avg_min": 60,
           "gpu_idle_util_pct": 5.0, "gpu_idle_mem_pct": 10.0}
    old.pop("spike_threshold_pct")
    r = Rules.from_dict(old)
    assert r.spike_threshold_pct == 40.0
    assert not hasattr(r, "share_avg_min")
    assert Rules.from_dict({"share_threshold_pct": 40.0, "spike_threshold_pct": 70.0}
                           ).spike_threshold_pct == 70.0


def test_over_threshold_excludes_mismatched_gpu_snapshots():
    assert not is_over_threshold(snaps(99, 99, gpus=[(0, 0)] * 2, minutes=15), now, R, 15, 1)


def test_spike_decision_debounce():
    assert spike_decision(False, True) == (True, True)
    assert spike_decision(True, True) == (False, True)
    assert spike_decision(True, False) == (False, False)
    assert spike_decision(False, False) == (False, False)
