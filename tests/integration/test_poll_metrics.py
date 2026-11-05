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
    if "badhost" in expr:
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
    bad = await make_server(name="bad", kind="grafana", grafana_instance="badhost")
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


def inf_side(request):
    expr = json.loads(request.content)["queries"][0]["expr"]
    v = "+Inf" if "badhost" in expr else 7.0
    return httpx.Response(200, json={"results": {"A": {"frames": [frame(v)]}}})


async def test_poll_inf_value_fails_server_others_saved(db, store, make_server):
    await configure(store)
    bad = await make_server(name="bad", kind="grafana", grafana_instance="badhost")
    good = await make_server(name="good", kind="grafana", grafana_instance="good-host")
    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(side_effect=inf_side)
        res = await poll_metrics(db, store, factory, NOW)
    assert res.ok == 1 and list(res.failed) == ["bad"]
    assert await usage.latest(db, good.id) is not None


async def test_poll_flush_failure_isolated(db, store, make_server, monkeypatch):
    from sqlalchemy.exc import DBAPIError
    await configure(store)
    bad = await make_server(name="bad", kind="grafana", grafana_instance="badhost")
    good = await make_server(name="good", kind="grafana", grafana_instance="good-host")
    real = usage.save_snapshot

    async def flaky(db_, server_id, u):
        if server_id == bad.id:
            raise DBAPIError("stmt", {}, Exception("boom"))
        await real(db_, server_id, u)

    monkeypatch.setattr(usage, "save_snapshot", flaky)
    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(
            return_value=httpx.Response(200, json={"results": {"A": {"frames": [frame(7.0)]}}}))
        res = await poll_metrics(db, store, factory, NOW)
    assert res.ok == 1 and list(res.failed) == ["bad"]
    assert await usage.latest(db, good.id) is not None
    assert (await store.get("health.last_poll"))["ok"] == 1


async def test_poll_http_500_reported_failed(db, store, make_server):
    await configure(store)
    await make_server(name="s", kind="grafana", grafana_instance="s")
    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(return_value=httpx.Response(500))
        res = await poll_metrics(db, store, factory, NOW)
    assert res.ok == 0 and "500" in res.failed["s"]


async def test_poll_failure_messages_never_store_credentials(db, store, make_server):
    await configure(store)
    await store.set(K.GRAFANA_PASSWORD, "hunter2", secret=True)
    await make_server(name="m", kind="grafana", grafana_instance="m")

    async def creds_factory(_store):
        return GrafanaClient("http://viewer:hunter2@grafana.test", None, None, None)

    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(
            side_effect=httpx.ConnectError("refused http://viewer:hunter2@grafana.test token=hunter2"))
        res = await poll_metrics(db, store, creds_factory, NOW)
    stored = (await store.get("health.last_poll"))["failed"]["m"]
    assert "refused" in stored and "hunter2" not in stored and "viewer" not in stored
    assert "hunter2" not in res.failed["m"]


async def test_poll_keeps_last_ok_at(db, store, make_server):
    await configure(store)
    s = await make_server(name="good", kind="grafana", grafana_instance="good-host")
    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(side_effect=side)
        await poll_metrics(db, store, factory, NOW)
    assert (await store.get("health.last_poll"))["last_ok_at"] == NOW.isoformat()

    s.grafana_instance = "badhost"  # every server fails now
    later = NOW + timedelta(minutes=5)
    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(side_effect=side)
        res = await poll_metrics(db, store, factory, later)
    assert res.ok == 0
    health = await store.get("health.last_poll")
    assert health["at"] == later.isoformat() and health["last_ok_at"] == NOW.isoformat()

    async def none(_):
        return None
    await poll_metrics(db, store, none, later + timedelta(minutes=5))
    assert (await store.get("health.last_poll"))["last_ok_at"] == NOW.isoformat()


async def test_poll_uses_new_gpu_defaults_over_stored_legacy_defaults(db, store, make_server):
    from app.integrations.grafana import LEGACY_DEFAULT_QUERIES
    await configure(store)
    # saved verbatim by the old form: DCGM GPU queries, case-sensitive cpu/ram
    await store.set(K.GRAFANA_QUERIES, {k: v[0] for k, v in LEGACY_DEFAULT_QUERIES.items()})
    s = await make_server(name="g", kind="grafana", grafana_instance="g-host",
                            has_gpu=True, gpu_count=1)
    seen = []

    def record(request):
        seen.append(json.loads(request.content)["queries"][0]["expr"])
        return side(request)

    with respx.mock:
        respx.post(f"{BASE}/api/ds/query").mock(side_effect=record)
        res = await poll_metrics(db, store, factory, NOW)
    assert res.ok == 1, res.failed
    assert not any("nvidia_smi" in e or "Hostname" in e for e in seen)
    assert any(e.startswith("DCGM_FI_DEV_GPU_UTIL{instance=~") for e in seen)
    assert all(r"(?i)g\\-host" in e for e in seen)  # PromQL text: g\\-host
    assert len((await usage.latest(db, s.id)).gpus) == 1
