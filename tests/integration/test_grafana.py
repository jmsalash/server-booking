import base64
from datetime import datetime, timezone

import httpx
import pytest
import respx

from app.integrations.grafana import (
    DEFAULT_QUERIES, GrafanaClient, GrafanaError, MetricsUnavailable, fetch_usage,
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
