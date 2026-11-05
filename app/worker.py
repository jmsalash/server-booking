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
from app.jobs import lifecycle, membership, metrics, monitor
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

    # ---- reconcile -------------------------------------------------------
    async def reconcile(self, now: datetime) -> None:
        async with self.sessionmaker() as db:
            store = SettingsStore(db, self.box)
            token = await store.get(K.TG_TOKEN)
            gr = [await store.get(k) for k in (
                K.GRAFANA_URL, K.GRAFANA_USER, K.GRAFANA_PASSWORD, K.GRAFANA_TOKEN,
                K.GRAFANA_DS_UID)]
            gcal = [await store.get(K.GCAL_SA_JSON), await store.get(K.GCAL_CALENDAR_ID)]
        for name, fn, args in (
            ("bot", self._reconcile_bot, (token,)),
            ("grafana", self._reconcile_grafana, (gr,)),
            ("gcal", self._reconcile_gcal, (gcal,)),
        ):
            try:
                await fn(*args)
            except Exception:
                log.exception("reconcile %s failed; will retry", name)

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
        # idle_check needs fresh snapshots, so it runs after the poll; spike_check last.
        sm = self.sessionmaker
        await run_job("poll_metrics", self._poll, sm)
        await run_job("idle_check", self._store_job(monitor.idle_check), sm)
        await run_job("spike_check", self._store_job(monitor.spike_check), sm)

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
        await self.reconcile(utcnow())
        self.scheduler = self.build_scheduler()
        self.scheduler.start()
        try:
            await stop.wait()
        finally:
            self.scheduler.shutdown(wait=False)
            await self._stop_bot()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    box = SecretBox(load_or_create_key(data_dir / "secret.key"))
    engine = make_engine(os.environ["DATABASE_URL"])
    try:
        await Worker(make_sessionmaker(engine), box).run()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
