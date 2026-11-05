from datetime import datetime, timezone

import pytest
from sqlalchemy import select

import app.worker as worker_mod
from app.models import OutboxItem
from app.services import calendar_sync, outbox
from app.settings_store import K, SettingsStore
from app.worker import Worker
from tests.fakes import FakeTelegram

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


class FakeBot:
    username = "lab_bot"

    async def get_me(self):
        return type("Me", (), {"id": 1, "username": "b"})()


class FakeUpdater:
    def __init__(self, log):
        self.log = log

    async def start_polling(self):
        self.log.append("polling")

    async def stop(self):
        self.log.append("updater_stop")


class FakeApp:
    def __init__(self, token, log):
        self.token = token
        self.log = log
        self.bot = FakeBot()
        self.updater = FakeUpdater(log)

    async def initialize(self):
        self.log.append(("initialize", self.token))

    async def start(self):
        self.log.append(("start", self.token))

    async def stop(self):
        self.log.append(("stop", self.token))

    async def shutdown(self):
        self.log.append(("shutdown", self.token))


@pytest.fixture
def app_log(monkeypatch):
    log = []
    monkeypatch.setattr(worker_mod, "build_application", lambda token, ctx: FakeApp(token, log))
    return log


@pytest.fixture
def worker(sessionmaker, box, app_log):
    return Worker(sessionmaker, box)


async def _set_token(sessionmaker, box, token):
    async with sessionmaker() as s:
        await SettingsStore(s, box).set(K.TG_TOKEN, token, secret=True)
        await s.commit()


async def test_reconcile_without_token_starts_no_bot(worker, app_log):
    await worker.reconcile(NOW)
    assert worker.bot_app is None
    assert app_log == []


async def test_reconcile_starts_and_restarts_bot_on_token_change(
    worker, app_log, sessionmaker, box
):
    await _set_token(sessionmaker, box, "tok1")
    await worker.reconcile(NOW)
    assert worker.bot_app.token == "tok1"
    assert [e for e in app_log if isinstance(e, tuple) and e[0] == "start"] == [("start", "tok1")]

    await worker.reconcile(NOW)  # unchanged: no restart
    assert sum(1 for e in app_log if e == ("start", "tok1")) == 1
    assert ("stop", "tok1") not in app_log

    await _set_token(sessionmaker, box, "tok2")
    await worker.reconcile(NOW)
    assert worker.bot_app.token == "tok2"
    assert ("stop", "tok1") in app_log and ("shutdown", "tok1") in app_log
    assert app_log.index(("shutdown", "tok1")) < app_log.index(("initialize", "tok2"))
    assert ("start", "tok2") in app_log


async def test_failing_bot_start_is_retried_not_raised(worker, app_log, sessionmaker, box, monkeypatch):
    await _set_token(sessionmaker, box, "bad")

    class Broken(FakeApp):
        async def initialize(self):
            raise RuntimeError("invalid token")

    monkeypatch.setattr(worker_mod, "build_application", lambda t, c: Broken(t, app_log))
    await worker.reconcile(NOW)
    assert worker.bot_app is None
    monkeypatch.setattr(worker_mod, "build_application", lambda t, c: FakeApp(t, app_log))
    await worker.reconcile(NOW)
    assert worker.bot_app.token == "bad"


async def test_outbox_runs_without_calendar_configured(worker, db, monkeypatch, sessionmaker, box):
    fake = FakeTelegram()
    monkeypatch.setattr(worker_mod, "PtbGateway", lambda bot: fake)
    await _set_token(sessionmaker, box, "tok1")
    await worker.reconcile(NOW)
    await outbox.enqueue(db, "telegram_message", {"chat_id": 5, "text": "hi"}, NOW)
    await outbox.enqueue(db, calendar_sync.CREATE, {"booking_id": 1}, NOW)
    await db.flush()
    await worker.process_outbox(db, NOW)
    assert [m["text"] for m in fake.sent] == ["hi"]
    items = {i.kind: i for i in (await db.scalars(select(OutboxItem))).all()}
    assert items["telegram_message"].done_at is not None
    assert items[calendar_sync.CREATE].done_at is None and items[calendar_sync.CREATE].attempts == 0


async def test_telegram_items_wait_when_no_bot(worker, db):
    await outbox.enqueue(db, "telegram_message", {"chat_id": 5, "text": "hi"}, NOW)
    await db.flush()
    await worker.process_outbox(db, NOW)
    item = (await db.scalars(select(OutboxItem))).one()
    assert item.done_at is None


async def test_corrupt_secret_does_not_block_reconcile(worker, app_log, sessionmaker, box):
    from app.models import Setting

    await _set_token(sessionmaker, box, "tok1")
    async with sessionmaker() as s:
        s.add(Setting(key=K.GRAFANA_PASSWORD, value={"enc": "garbage"}, is_secret=True))
        await s.commit()
    await worker.reconcile(NOW)
    assert worker.bot_app.token == "tok1"
    assert worker.grafana is None


async def _heartbeat(sessionmaker, box):
    async with sessionmaker() as s:
        return await SettingsStore(s, box).get(K.WORKER_HEARTBEAT)


async def test_reconcile_writes_heartbeat(worker, sessionmaker, box):
    await worker.reconcile(NOW)
    assert await _heartbeat(sessionmaker, box) == {
        "at": NOW.isoformat(), "bot_running": False, "bot_username": None,
    }
    await _set_token(sessionmaker, box, "tok1")
    later = NOW.replace(minute=1)
    await worker.reconcile(later)
    assert await _heartbeat(sessionmaker, box) == {
        "at": later.isoformat(), "bot_running": True, "bot_username": "lab_bot",
    }


async def test_heartbeat_failure_is_logged_not_raised(worker, monkeypatch, caplog):
    async def broken(self, key, value, secret=False):
        raise RuntimeError("db down")

    monkeypatch.setattr(SettingsStore, "set", broken)
    await worker.reconcile(NOW)  # does not raise
    assert "heartbeat" in caplog.text


def test_scheduler_has_housekeeping_job(worker):
    sched = worker.build_scheduler()
    assert sched.get_job("prune_housekeeping") is not None
    assert sched.get_job("prune_snapshots") is not None
