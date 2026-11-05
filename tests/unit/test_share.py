from datetime import timedelta

from app.domain.rules import Rules
from app.domain.share import is_over_threshold, is_shareable_now, is_stale, spike_decision
from app.domain.timeutil import parse_local
from app.domain.types import GpuUsage, ServerInfo, ServerUsage

R = Rules()
now = parse_local("2026-11-10T12:20", R.timezone)


def server(kind="grafana", gpus=0, shareable=True):
    return ServerInfo(1, "s", kind, gpus > 0, gpus, shareable, True)


cpu_server = server()
gpu1 = server(gpus=1)
gpu4 = server(gpus=4)


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


def test_cpu_server_under_50_shareable():
    assert is_shareable_now(cpu_server, 1, snaps(30, 40), now, R)


def test_ram_over_50_not_shareable():
    assert not is_shareable_now(cpu_server, 1, snaps(30, 60), now, R)


def test_cpu_over_50_not_shareable():
    assert not is_shareable_now(cpu_server, 1, snaps(60, 10), now, R)


def test_threshold_is_strict():
    assert not is_shareable_now(cpu_server, 1, snaps(50, 10), now, R)


def test_multi_gpu_half_idle_shareable():
    g = [(90, 80), (90, 80), (0, 1), (2, 3)]
    assert is_shareable_now(gpu4, 1, snaps(20, 30, gpus=g), now, R)


def test_multi_gpu_three_busy_not_shareable():
    g = [(90, 80), (90, 80), (50, 60), (2, 3)]
    assert not is_shareable_now(gpu4, 1, snaps(20, 30, gpus=g), now, R)


def test_single_gpu_low_memory_shareable_even_if_util_high():
    assert is_shareable_now(gpu1, 1, snaps(20, 30, gpus=[(95, 30)]), now, R)


def test_single_gpu_memory_over_half_not_shareable():
    assert not is_shareable_now(gpu1, 1, snaps(20, 30, gpus=[(5, 70)]), now, R)


def test_stale_not_shareable():
    s = snaps(10, 10, end=now - timedelta(minutes=20))
    assert not is_shareable_now(cpu_server, 1, s, now, R)


def test_empty_not_shareable():
    assert not is_shareable_now(cpu_server, 1, [], now, R)


def test_standalone_never_shareable():
    assert not is_shareable_now(server(kind="standalone"), 1, snaps(10, 10), now, R)
    assert not is_shareable_now(server(shareable=False), 1, snaps(10, 10), now, R)


def test_gpu_count_mismatch_is_missing_data():
    assert not is_shareable_now(gpu4, 1, snaps(20, 30, gpus=[(0, 0)] * 3), now, R)
    assert not is_shareable_now(gpu1, 1, snaps(20, 30), now, R)


def test_zero_or_two_active_bookings_not_shareable():
    assert not is_shareable_now(cpu_server, 0, snaps(10, 10), now, R)
    assert not is_shareable_now(cpu_server, 2, snaps(10, 10), now, R)


def test_old_snapshots_outside_window_ignored():
    old = snaps(90, 90, minutes=120, end=now - timedelta(minutes=61))
    assert is_shareable_now(cpu_server, 1, old + snaps(10, 10, minutes=55), now, R)


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


def test_over_threshold_excludes_mismatched_gpu_snapshots():
    assert not is_over_threshold(snaps(99, 99, gpus=[(0, 0)] * 2, minutes=15), now, R, 15, 1)


def test_spike_decision_debounce():
    assert spike_decision(False, True) == (True, True)
    assert spike_decision(True, True) == (False, True)
    assert spike_decision(True, False) == (False, False)
    assert spike_decision(False, False) == (False, False)
