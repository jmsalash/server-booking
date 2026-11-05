import math
import re
from dataclasses import dataclass
from datetime import datetime

import httpx

from app.domain.types import GpuUsage, ServerUsage

DEFAULT_QUERIES: dict[str, str] = {
    "cpu": '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle",instance=~"{instance_re}(:.*)?"}[5m])))',
    "ram": '100 * (1 - node_memory_MemAvailable_bytes{instance=~"{instance_re}(:.*)?"} / node_memory_MemTotal_bytes{instance=~"{instance_re}(:.*)?"})',
    "gpu_util": 'DCGM_FI_DEV_GPU_UTIL{Hostname="{instance}"}',
    "gpu_mem": '100 * DCGM_FI_DEV_FB_USED{Hostname="{instance}"} / (DCGM_FI_DEV_FB_USED{Hostname="{instance}"} + DCGM_FI_DEV_FB_FREE{Hostname="{instance}"})',
}


REDACTED = "••••"


def redact(message: str, secrets=()) -> str:
    """Remove known secret values and URL credentials (scheme://user:pw@) from a message."""
    for s in secrets:
        if s:
            message = message.replace(s, REDACTED)
    return re.sub(r"(://)[^/@\s]+@", rf"\1{REDACTED}@", message)


class GrafanaError(Exception):
    """HTTP/transport/protocol failure talking to Grafana."""


class MetricsUnavailable(Exception):
    """Expected series missing or inconsistent."""


@dataclass(frozen=True)
class Datasource:
    uid: str
    name: str
    type: str


@dataclass(frozen=True)
class Series:
    labels: dict[str, str]
    value: float


class GrafanaClient:
    def __init__(self, base_url: str, username: str | None = None, password: str | None = None,
                 token: str | None = None, timeout: float = 10):
        headers = {}
        auth = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif username:
            auth = (username, password or "")
        self._base_url = base_url.rstrip("/")
        self._headers = headers
        self._auth = auth
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, headers=self._headers,
                                 auth=self._auth, timeout=self._timeout)

    async def _request(self, method: str, path: str, **kw):
        try:
            async with self._client() as c:
                r = await c.request(method, path, **kw)
                r.raise_for_status()
                return r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise GrafanaError(f"{type(e).__name__}: {e}") from e

    async def list_datasources(self) -> list[Datasource]:
        data = await self._request("GET", "/api/datasources")
        return [Datasource(d["uid"], d["name"], d["type"]) for d in data]

    async def instant_query(self, ds_uid: str, expr: str) -> list[Series]:
        body = {
            "queries": [{"refId": "A", "datasource": {"uid": ds_uid}, "expr": expr, "instant": True}],
            "from": "now-5m",
            "to": "now",
        }
        data = await self._request("POST", "/api/ds/query", json=body)
        out: list[Series] = []
        try:
            frames = data["results"]["A"].get("frames", [])
            for f in frames:
                fields = f["schema"]["fields"]
                if len(fields) < 2:
                    continue
                values = f.get("data", {}).get("values", [])
                if len(values) < 2 or not values[1]:
                    continue
                try:
                    v = float(values[1][-1])
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(v):
                    continue
                labels = {str(k): str(x) for k, x in (fields[1].get("labels") or {}).items()}
                out.append(Series(labels, v))
        except (KeyError, TypeError, AttributeError) as e:
            raise GrafanaError(f"unexpected response shape: {e!r}") from e
        return out


def _gpu_key(s: Series):
    for name in ("gpu", "minor_number"):
        raw = s.labels.get(name)
        if raw is not None:
            if re.fullmatch(r"\d+", raw):
                return (0, int(raw), "")
            return (1, 0, raw)
    return (2, 0, "&".join(f"{k}={v}" for k, v in sorted(s.labels.items())))


def _expand(q: str, instance: str) -> str:
    # {instance} is substituted raw; {instance_re} is regex-escaped (and backslashes
    # doubled, since it lands inside a double-quoted PromQL string) for =~ matchers.
    inst_re = re.escape(instance).replace("\\", "\\\\")
    return q.replace("{instance_re}", inst_re).replace("{instance}", instance)


def _single(series: list[Series], what: str, instance: str) -> Series:
    if not series:
        raise MetricsUnavailable(f"no {what} series for {instance}")
    if len(series) > 1:
        raise MetricsUnavailable(f"{len(series)} {what} series for {instance}, expected 1")
    return series[0]


async def fetch_usage(client: GrafanaClient, ds_uid: str, queries: dict[str, str], instance: str,
                      gpu_count: int, now: datetime) -> ServerUsage:
    async def one(key: str) -> list[Series]:
        return await client.instant_query(ds_uid, _expand(queries[key], instance))

    cpu = _single(await one("cpu"), "CPU", instance)
    ram = _single(await one("ram"), "RAM", instance)
    gpus: tuple[GpuUsage, ...] = ()
    if gpu_count > 0:
        util = sorted(await one("gpu_util"), key=_gpu_key)
        mem = sorted(await one("gpu_mem"), key=_gpu_key)
        if len(util) != gpu_count or len(mem) != gpu_count:
            raise MetricsUnavailable(
                f"expected {gpu_count} GPUs for {instance}, got util={len(util)} mem={len(mem)}"
            )
        for group in (util, mem):
            keys = [_gpu_key(x) for x in group]
            if len(set(keys)) != len(keys):
                raise MetricsUnavailable(f"duplicate GPU labels for {instance}")
        gpus = tuple(GpuUsage(u.value, m.value) for u, m in zip(util, mem))
    return ServerUsage(ts=now, cpu_pct=cpu.value, ram_pct=ram.value, gpus=gpus)
