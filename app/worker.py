import asyncio
import hashlib
import logging
import os
import signal
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import BotContext, build_application
from app.clock import utcnow
from app.crypto import SecretBox, load_or_create_key
from app.db import make_engine, make_sessionmaker
from app.integrations.gcal import GoogleCalendarClient
from app.integrations.grafana import GrafanaClient
from app.integrations.telegram_gateway import PtbGateway
from app.jobs import lifecycle, maintenance, membership, metrics, monitor
from app.jobs.runner import run_job
from app.services import calendar_sync, notify, outbox
from app.settings_store import K, SettingsStore

log = logging.getLogger(__name__)


def _h(*parts) -> str:
    return hashlib.sha256("\x00".join(str(p) for p in parts).encode()).hexdigest()


class Worker:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], box: SecretBox):
        self.sessionmaker = sessionmaker
        self.box = box
        self.bot_app = None
        self.gateway: PtbGateway | None = None
        self.grafana: GrafanaClient | None = None
        self.calendar_handlers: dict = {}
        self._fp: dict[str, str | None] = {"bot": None, "grafana": None, "gcal": None}
        self.scheduler: AsyncIOScheduler | None = None
        self._failed: dict[str, str] = {}
        self._heartbeat_failed = False

    # ---- reconcile -------------------------------------------------------
    async def _read(self, *keys: str) -> list | None:
        """Read settings; an unreadable one (e.g. undecryptable secret) -> None, logged by key only."""
        out = []
        async with self.sessionmaker() as db:
            store = SettingsStore(db, self.box)
            for k in keys:
                try:
                    out.append(await store.get(k))
                except Exception as e:  # noqa: BLE001
                    log.error("setting %s unreadable (%s); treating component as not configured",
                              k, type(e).__name__)
                    return None
        return out

    async def reconcile(self, now: datetime) -> None:
        components = (
            ("bot", self._reconcile_bot, (K.TG_TOKEN,)),
            ("grafana", self._reconcile_grafana, (
                K.GRAFANA_URL, K.GRAFANA_USER, K.GRAFANA_PASSWORD, K.GRAFANA_TOKEN,
                K.GRAFANA_DS_UID)),
            ("gcal", self._reconcile_gcal, (K.GCAL_SA_JSON, K.GCAL_CALENDAR_ID)),
        )
        for name, fn, keys in components:
            try:
                values = await self._read(*keys)
                if values is None:
                    values = [None] * len(keys)
                await fn(*([values[0]] if name == "bot" else [values]))
            except Exception as e:  # noqa: BLE001
                fp = _h(name, *(values if values else ()))
                if self._failed.get(name) != fp:  # log once per settings change
                    self._failed[name] = fp
                    log.error("reconcile %s failed (%s); will retry", name, type(e).__name__)
            else:
                self._failed.pop(name, None)
        await self._heartbeat(now)

    def _bot_username(self) -> str | None:
        try:
            return self.bot_app.bot.username if self.bot_app is not None else None
        except Exception:  # noqa: BLE001  not initialised / unexpected bot object
            return None

    async def _heartbeat(self, now: datetime) -> None:
        """Tell the web health page the worker is alive; own transaction, never raises."""
        try:
            async with self.sessionmaker() as db:
                await SettingsStore(db, self.box).set(K.WORKER_HEARTBEAT, {
                    "at": now.isoformat(), "bot_running": self.bot_app is not None,
                    "bot_username": self._bot_username(),
                })
                await db.commit()
        except Exception as e:  # noqa: BLE001
            if not self._heartbeat_failed:  # log once until it works again
                log.error("worker heartbeat failed (%s)", type(e).__name__)
            self._heartbeat_failed = True
        else:
            self._heartbeat_failed = False

    async def _reconcile_bot(self, token: str | None) -> None:
        fp = _h(token) if token else None
        if fp == self._fp["bot"]:
            return
        await self._stop_bot()
        self._fp["bot"] = None
        if not token:
            self._fp["bot"] = None
            return
        gateway = PtbGateway(None)
        app = build_application(token, BotContext(self.sessionmaker, gateway, self.box))
        gateway.bot = app.bot
        try:
            await app.initialize()  # validates the token (get_me)
            await app.start()
            await app.updater.start_polling()
        except Exception:
            await self._shutdown(app)
            raise
        self.bot_app, self.gateway, self._fp["bot"] = app, gateway, fp

    async def _shutdown(self, app) -> None:
        for step in (app.updater.stop, app.stop, app.shutdown):
            try:
                await step()
            except Exception:
                log.debug("bot shutdown step failed", exc_info=True)

    async def _stop_bot(self) -> None:
        app, self.bot_app, self.gateway = self.bot_app, None, None
        if app is not None:
            await self._shutdown(app)

    async def _reconcile_grafana(self, gr: list) -> None:
        url, user, password, token, ds_uid = gr
        fp = _h(*gr) if url and ds_uid else None
        if fp == self._fp["grafana"]:
            return
        old, self.grafana = self.grafana, (
            GrafanaClient(url, user, password, token) if fp else None
        )
        self._fp["grafana"] = fp
        if old is not None and hasattr(old, "aclose"):
            await old.aclose()

    async def _reconcile_gcal(self, gcal: list) -> None:
        sa_json, cal_id = gcal
        fp = _h(*gcal) if sa_json and cal_id else None
        if fp == self._fp["gcal"]:
            return
        if fp:
            client = await asyncio.to_thread(GoogleCalendarClient, sa_json, cal_id)
            self.calendar_handlers = calendar_sync.make_handlers(client)
        else:
            self.calendar_handlers = {}
        self._fp["gcal"] = fp

    # ---- jobs ------------------------------------------------------------
    async def process_outbox(self, db: AsyncSession, now: datetime) -> int:
        handlers = dict(self.calendar_handlers)
        if self.gateway is not None:
            handlers["telegram_message"] = notify.make_telegram_handler(self.gateway)
        return await outbox.process_due(db, handlers, now)

    async def _poll(self, db, now):
        async def factory(_store):
            return self.grafana

        await metrics.poll_metrics(db, SettingsStore(db, self.box), factory, now)

    async def _membership(self, db, now):
        if self.gateway is None:
            return
        await membership.membership_sync(db, SettingsStore(db, self.box), self.gateway, now)

    def _store_job(self, fn):
        async def job(db, now):
            await fn(db, SettingsStore(db, self.box), now)

        return job

    async def metrics_and_idle(self) -> None:
        # Both checks need fresh snapshots, so they run after the poll.
        sm = self.sessionmaker
        await run_job("poll_metrics", self._poll, sm)
        await run_job("idle_check", self._store_job(monitor.idle_check), sm)
        await run_job("full_check", self._store_job(monitor.full_check), sm)

    def build_scheduler(self) -> AsyncIOScheduler:
        sm = self.sessionmaker
        sched = AsyncIOScheduler(job_defaults={"max_instances": 1, "coalesce": True})

        async def reconcile():
            await run_job("reconcile", lambda db, now: self.reconcile(now), sm)

        def job(name, fn):
            async def wrapped():
                await run_job(name, fn, sm)

            return wrapped

        sched.add_job(reconcile, "interval", seconds=10, id="reconcile")
        sched.add_job(self.metrics_and_idle, "interval", minutes=5, id="metrics_and_idle")
        sched.add_job(job("booking_lifecycle", self._store_job(lifecycle.booking_lifecycle)),
                      "interval", minutes=1, id="booking_lifecycle")
        sched.add_job(job("outbox", self.process_outbox), "interval", seconds=15, id="outbox")
        sched.add_job(job("membership_sync", self._membership), "interval", hours=1,
                      id="membership_sync")
        sched.add_job(job("prune_snapshots", self._store_job(metrics.prune_snapshots)),
                      "interval", hours=1, id="prune_snapshots")
        sched.add_job(job("prune_housekeeping", self._store_job(maintenance.prune_housekeeping)),
                      "interval", hours=1, id="prune_housekeeping")
        return sched

    # ---- lifecycle -------------------------------------------------------
    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        try:
            await self.reconcile(utcnow())
        except Exception as e:  # noqa: BLE001
            log.error("startup reconcile failed (%s)", type(e).__name__)
        self.scheduler = self.build_scheduler()
        self.scheduler.start()
        try:
            await stop.wait()
        finally:
            self.scheduler.shutdown(wait=False)
            await self._stop_bot()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    for noisy in ("httpx", "httpcore"):  # their INFO logs contain URLs with the bot token
        logging.getLogger(noisy).setLevel(logging.WARNING)
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    box = SecretBox(load_or_create_key(data_dir / "secret.key"))
    engine = make_engine(os.environ["DATABASE_URL"])
    try:
        await Worker(make_sessionmaker(engine), box).run()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
