from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.grafana import (
    GrafanaClient, GrafanaError, MetricsUnavailable, effective_queries, fetch_usage, redact,
)
from app.models import Server
from app.services import usage as usage_svc
from app.settings_store import K, SettingsStore

HEALTH_KEY = "health.last_poll"


@dataclass
class PollResult:
    ok: int
    failed: dict[str, str] = field(default_factory=dict)


async def default_client_factory(store: SettingsStore) -> GrafanaClient | None:
    url = await store.get(K.GRAFANA_URL)
    if not url or not await store.get(K.GRAFANA_DS_UID):
        return None
    return GrafanaClient(
        url,
        await store.get(K.GRAFANA_USER),
        await store.get(K.GRAFANA_PASSWORD),
        await store.get(K.GRAFANA_TOKEN),
    )


async def poll_metrics(
    db: AsyncSession,
    store: SettingsStore,
    client_factory: Callable[[SettingsStore], Awaitable[GrafanaClient | None]],
    now: datetime,
) -> PollResult:
    previous = await store.get(HEALTH_KEY) or {}
    last_ok = {"last_ok_at": previous["last_ok_at"]} if previous.get("last_ok_at") else {}
    client = await client_factory(store)
    if client is None:
        await store.set(HEALTH_KEY, {"at": now.isoformat(), "ok": 0, "failed": {},
                                     "note": "Grafana not configured", **last_ok})
        return PollResult(0, {})
    ds_uid = await store.get(K.GRAFANA_DS_UID)
    queries = effective_queries(await store.get(K.GRAFANA_QUERIES))
    servers = (await db.scalars(
        select(Server)
        .where(Server.kind == "grafana", Server.enabled.is_(True), Server.archived_at.is_(None))
        .order_by(Server.id)
    )).all()
    ok = 0
    failed: dict[str, str] = {}
    secrets = (await store.get(K.GRAFANA_PASSWORD), await store.get(K.GRAFANA_TOKEN))
    for s in servers:
        try:
            u = await fetch_usage(client, ds_uid, queries, s.grafana_instance or s.hostname,
                                  s.gpu_count, now)
            async with db.begin_nested():
                await usage_svc.save_snapshot(db, s.id, u)
            ok += 1
        except (GrafanaError, MetricsUnavailable, SQLAlchemyError) as e:
            failed[s.name] = redact(str(e), secrets)  # never persist credentials
    if ok > 0:
        last_ok = {"last_ok_at": now.isoformat()}
    await store.set(HEALTH_KEY, {"at": now.isoformat(), "ok": ok, "failed": failed, **last_ok})
    return PollResult(ok, failed)


async def prune_snapshots(db: AsyncSession, store: SettingsStore, now: datetime) -> int:
    rules = await store.get_rules()
    return await usage_svc.prune(db, now - timedelta(hours=rules.snapshot_retention_h))
