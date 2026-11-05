import base64
from datetime import datetime, timezone

import httpx
import pytest
import respx

from app.integrations.grafana import (
    DEFAULT_QUERIES, DiscoveredHost, GrafanaClient, GrafanaError, MetricsUnavailable,
    discover_hosts, fetch_usage,
)

BASE = "http://grafana.test"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def frame(value, labels=None):
    return {
        "schema": {"fields": [{"name": "Time"}, {"name": "Value", "labels": labels or {}}]},
        "data": {"values": [[1, 2], [0, value]]},
    }


def reply(*frames):
    return httpx.Response(200, json={"results": {"A": {"frames": list(frames)}}})


def by_query(mapping):
    """respx side effect: pick a response by substring of the expr."""
    def side(request):
        import json
        expr = json.loads(request.content)["queries"][0]["expr"]
        for needle, frames in mapping:
            if needle in expr:
                return reply(*frames)
        return reply()
    return side


async def test_list_datasources_basic_auth():
    with respx.mock:
        route = respx.get(f"{BASE}/api/datasources").mock(
            return_value=httpx.Response(200, json=[{"uid": "p1", "name": "Prom", "type": "prometheus"}])
        )
        ds = await GrafanaClient(BASE, "bob", "pw", None).list_datasources()
    assert ds[0].uid == "p1" and ds[0].type == "prometheus"
    expected = "Basic " + base64.b64encode(b"bob:pw").decode()
    assert route.calls[0].request.headers["Authorization"] == expected


async def test_token_auth_header():
    with respx.mock:
        route = respx.get(f"{BASE}/api/datasources").mock(return_value=httpx.Response(200, json=[]))
        await GrafanaClient(BASE, "bob", "pw", "tok").list_datasources()
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"


async def test_fetch_usage_parses_frames():
    mapping = [
        ("node_cpu_seconds_total", [frame(12.5)]),
        ("node_memory_MemAvailable_bytes", [frame(40.0)]),
        ("FB_USED", [frame(30.0, {"gpu": "1"}), frame(10.0, {"gpu": "0"})]),
        ("GPU_UTIL", [frame(90.0, {"gpu": "1"}), frame(5.0, {"gpu": "0"})]),
    ]
    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(side_effect=by_query(mapping))
        u = await fetch_usage(GrafanaClient(BASE, None, None, "t"), "p1", DEFAULT_QUERIES, "srv", 2, NOW)
    assert u.ts == NOW and u.cpu_pct == 12.5 and u.ram_pct == 40.0
    assert [(g.util_pct, g.mem_pct) for g in u.gpus] == [(5.0, 10.0), (90.0, 30.0)]


async def test_empty_cpu_series_is_unavailable_not_zero():
    mapping = [("node_memory_MemAvailable_bytes", [frame(40.0)])]
    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(side_effect=by_query(mapping))
        with pytest.raises(MetricsUnavailable):
            await fetch_usage(GrafanaClient(BASE, None, None, "t"), "p1", DEFAULT_QUERIES, "srv", 0, NOW)


async def test_gpu_series_count_mismatch_unavailable():
    mapping = [
        ("node_cpu_seconds_total", [frame(1.0)]),
        ("node_memory_MemAvailable_bytes", [frame(2.0)]),
        ("FB_USED", [frame(30.0, {"gpu": "0"})]),
        ("GPU_UTIL", [frame(5.0, {"gpu": "0"})]),
    ]
    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(side_effect=by_query(mapping))
        with pytest.raises(MetricsUnavailable):
            await fetch_usage(GrafanaClient(BASE, None, None, "t"), "p1", DEFAULT_QUERIES, "srv", 2, NOW)


async def test_grafana_timeout_raises():
    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(side_effect=httpx.ConnectTimeout("slow"))
        with pytest.raises(GrafanaError):
            await GrafanaClient(BASE, None, None, "t").instant_query("p1", "up")


async def test_nan_value_counts_as_missing():
    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(return_value=reply(frame("NaN")))
        assert await GrafanaClient(BASE, None, None, "t").instant_query("p1", "up") == []


async def test_infinite_value_counts_as_missing():
    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(return_value=reply(frame("+Inf"), frame("-Inf")))
        assert await GrafanaClient(BASE, None, None, "t").instant_query("p1", "up") == []


async def test_multiple_cpu_series_unavailable():
    mapping = [("node_cpu_seconds_total", [frame(1.0), frame(2.0)]),
               ("node_memory_MemAvailable_bytes", [frame(3.0)])]
    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(side_effect=by_query(mapping))
        with pytest.raises(MetricsUnavailable):
            await fetch_usage(GrafanaClient(BASE, None, None, "t"), "p1", DEFAULT_QUERIES, "srv", 0, NOW)


async def test_duplicate_gpu_labels_unavailable():
    mapping = [
        ("node_cpu_seconds_total", [frame(1.0)]),
        ("node_memory_MemAvailable_bytes", [frame(2.0)]),
        ("FB_USED", [frame(30.0, {"gpu": "0"}), frame(31.0, {"gpu": "0"})]),
        ("GPU_UTIL", [frame(5.0, {"gpu": "0"}), frame(6.0, {"gpu": "1"})]),
    ]
    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(side_effect=by_query(mapping))
        with pytest.raises(MetricsUnavailable):
            await fetch_usage(GrafanaClient(BASE, None, None, "t"), "p1", DEFAULT_QUERIES, "srv", 2, NOW)


async def test_instance_regex_escaped_only_in_regex_matchers():
    seen = []

    def side(request):
        import json
        seen.append(json.loads(request.content)["queries"][0]["expr"])
        return reply(frame(1.0, {"gpu": "0"}))

    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(side_effect=side)
        await fetch_usage(GrafanaClient(BASE, None, None, "t"), "p1", DEFAULT_QUERIES, "a.b", 1, NOW)
    cpu = next(e for e in seen if "node_cpu" in e)
    util = next(e for e in seen if e.startswith("DCGM_FI_DEV_GPU_UTIL"))
    assert 'instance=~"a\\\\.b(:.*)?"' in cpu
    assert 'Hostname="a.b"' in util


# --- discovery ------------------------------------------------------------------------------

HOSTS_Q = "count by (instance)"
GPUS_Q = "count by (Hostname)"


async def _discover(mapping, gpu_status=200):
    def side(request):
        import json
        expr = json.loads(request.content)["queries"][0]["expr"]
        if GPUS_Q in expr and gpu_status != 200:
            return httpx.Response(gpu_status, json={"message": "boom"})
        for needle, frames in mapping:
            if needle in expr:
                return reply(*frames)
        return reply()

    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(side_effect=side)
        return await discover_hosts(GrafanaClient(BASE, None, None, "t"), "p1", DEFAULT_QUERIES)


def test_discovery_queries_have_defaults():
    assert DEFAULT_QUERIES["discover_hosts"] == (
        'count by (instance) (node_cpu_seconds_total{mode="idle"})')
    assert DEFAULT_QUERIES["discover_gpus"] == 'count by (Hostname) (DCGM_FI_DEV_GPU_UTIL)'


async def test_discover_strips_port_and_reads_gpu_count():
    hosts = await _discover([
        (HOSTS_Q, [frame(32, {"instance": "gpu-01:9100"}), frame(8, {"instance": "cpu-02:9100"})]),
        (GPUS_Q, [frame(4, {"Hostname": "gpu-01"})]),
    ])
    assert list(hosts) == [DiscoveredHost("cpu-02", "cpu-02", 0),
                           DiscoveredHost("gpu-01", "gpu-01", 4)]
    assert hosts.gpu_error is None


async def test_discover_gpu_match_case_insensitive_and_short_vs_fqdn():
    hosts = await _discover([
        (HOSTS_Q, [frame(1, {"instance": "GPU-01.lab.local:9100"}),
                   frame(1, {"instance": "gpu-02:9100"}),
                   frame(1, {"instance": "gpu-03.lab:9100"})]),
        (GPUS_Q, [frame(2, {"Hostname": "gpu-01"}),
                  frame(8, {"Hostname": "GPU-02.lab.local"}),
                  frame(1, {"Hostname": "gpu-03.LAB"})]),
    ])
    assert {h.instance: h.gpu_count for h in hosts} == {
        "GPU-01.lab.local": 2, "gpu-02": 8, "gpu-03.lab": 1}


async def test_discover_ip_instances_do_not_match_by_first_label():
    hosts = await _discover([
        (HOSTS_Q, [frame(1, {"instance": "10.0.0.5:9100"})]),
        (GPUS_Q, [frame(2, {"Hostname": "10"}), frame(4, {"Hostname": "10.0.0.6"})]),
    ])
    assert list(hosts) == [DiscoveredHost("10.0.0.5", "10.0.0.5", 0)]


async def test_discover_gpu_query_failure_gives_zero_and_note():
    hosts = await _discover([(HOSTS_Q, [frame(1, {"instance": "gpu-01:9100"})])], gpu_status=500)
    assert list(hosts) == [DiscoveredHost("gpu-01", "gpu-01", 0)]
    assert isinstance(hosts.gpu_error, GrafanaError)


async def test_discover_dedupes_and_sorts():
    hosts = await _discover([
        (HOSTS_Q, [frame(1, {"instance": "b:9100"}), frame(1, {"instance": "a:9100"}),
                   frame(1, {"instance": "b:9101"}), frame(1, {"instance": "B"}),
                   frame(1, {"instance": ""}), frame(1, {"job": "no-instance"})]),
    ])
    assert [h.instance for h in hosts] == ["a", "b"]


async def test_discover_nothing_returns_empty_list():
    hosts = await _discover([])
    assert list(hosts) == [] and hosts.gpu_error is None


async def test_discover_ignores_garbage_gpu_values():
    bad = {"schema": {"fields": [{"name": "Time"}, {"name": "Value", "labels": {"Hostname": "g"}}]},
           "data": {"values": [[1], ["NaN"]]}}
    hosts = await _discover([
        (HOSTS_Q, [frame(1, {"instance": "g:9100"}), frame(1, {"instance": "h:9100"})]),
        (GPUS_Q, [bad, frame(-3, {"Hostname": "h"})]),
    ])
    assert {h.instance: h.gpu_count for h in hosts} == {"g": 0, "h": 0}


async def test_discover_hosts_error_propagates():
    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(return_value=httpx.Response(502))
        with pytest.raises(GrafanaError):
            await discover_hosts(GrafanaClient(BASE, None, None, "t"), "p1", DEFAULT_QUERIES)
