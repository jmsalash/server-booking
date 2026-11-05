import json
from datetime import datetime, timedelta, timezone

import httpx
import respx

from app.integrations.grafana import GrafanaClient
from app.jobs.metrics import poll_metrics, prune_snapshots
from app.services import usage
from app.domain.types import GpuUsage, ServerUsage
from app.settings_store import K

BASE = "http://grafana.test"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def frame(value):
    return {
        "schema": {"fields": [{"name": "Time"}, {"name": "Value", "labels": {}}]},
        "data": {"values": [[1], [value]]},
    }


def side(request):
    expr = json.loads(request.content)["queries"][0]["expr"]
    if "bad-host" in expr:
        return httpx.Response(200, json={"results": {"A": {"frames": []}}})
    return httpx.Response(200, json={"results": {"A": {"frames": [frame(7.0)]}}})


async def factory(store):
    return GrafanaClient(BASE, None, None, "t")


async def configure(store):
    await store.set(K.GRAFANA_URL, BASE)
    await store.set(K.GRAFANA_DS_UID, "p1")


async def test_poll_metrics_one_server_failing_others_saved(db, store, make_server):
    await configure(store)
    good = await make_server(name="good", kind="grafana", grafana_instance="good-host")
    bad = await make_server(name="bad", kind="grafana", grafana_instance="bad-host")
    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(side_effect=side)
        res = await poll_metrics(db, store, factory, NOW)
    assert res.ok == 1 and list(res.failed) == ["bad"]
    assert (await usage.latest(db, good.id)).cpu_pct == 7.0
    assert await usage.latest(db, bad.id) is None
    health = await store.get("health.last_poll")
    assert health["ok"] == 1 and "bad" in health["failed"] and health["at"] == NOW.isoformat()


async def test_poll_skips_standalone_and_disabled(db, store, make_server):
    await configure(store)
    a = await make_server(name="a", kind="standalone")
    b = await make_server(name="b", kind="grafana", grafana_instance="b", enabled=False)
    with respx.mock:
        route = respx.post(f"{BASE}/api/ds/query").mock(side_effect=side)
        res = await poll_metrics(db, store, factory, NOW)
    assert res.ok == 0 and res.failed == {} and not route.called
    assert await usage.latest(db, a.id) is None and await usage.latest(db, b.id) is None


async def test_poll_not_configured(db, store):
    async def none(_):
        return None
    res = await poll_metrics(db, store, none, NOW)
    assert res.ok == 0 and res.failed == {}
    assert "not configured" in (await store.get("health.last_poll"))["note"]


async def test_prune_removes_older_than_48h(db, store, make_server):
    s = await make_server(name="p")
    old = ServerUsage(NOW - timedelta(hours=49), 1.0, 2.0, (GpuUsage(3.0, 4.0),))
    new = ServerUsage(NOW - timedelta(hours=1), 5.0, 6.0, ())
    await usage.save_snapshot(db, s.id, old)
    await usage.save_snapshot(db, s.id, new)
    assert await prune_snapshots(db, store, NOW) == 1
    rec = await usage.recent(db, s.id, NOW - timedelta(days=5))
    assert rec == [new]
    assert (await usage.latest(db, s.id)) == new
