import secrets
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

import app.clock
from app.models import AuditLog, Session
from app.services import setup
from app.settings_store import K, SettingsStore
from tests.fakes import FakeTelegram

NOW = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def store(db, web_app):
    """Settings written by the test must be readable by the app (same encryption key)."""
    return SettingsStore(db, web_app.state.box)


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(app.clock, "utcnow", lambda: state["now"])
    return state


@pytest.fixture(autouse=True)
def fake_telegram(web_app):
    fake = FakeTelegram()
    web_app.state.telegram_factory = lambda token: fake
    return fake


@pytest_asyncio.fixture
async def code(db, store):
    c = await setup.ensure_setup_code(db, store, NOW, reset=False)
    await db.commit()
    return c


async def enter_code(client, code):
    r = await client.post("/admin/setup", data={"code": code})
    assert r.status_code == 303 and r.headers["location"] == "/admin/setup/telegram"
    return r


async def fresh(db, store, key, default=None):
    db.expire_all()
    return await store.get(key, default)


class FailingTelegram(FakeTelegram):
    async def get_me(self):
        raise RuntimeError("Unauthorized")


async def test_admin_requires_setup_code_before_setup(client):
    r = await client.get("/admin")
    assert r.status_code == 303 and r.headers["location"] == "/admin/setup"
    r = await client.get("/admin/servers")
    assert r.headers["location"] == "/admin/setup"


async def test_wizard_steps_require_code(client, code):
    r = await client.get("/admin/setup/telegram")
    assert r.status_code == 303 and r.headers["location"] == "/admin/setup"
    page = await client.get("/admin/setup")
    assert page.status_code == 200 and 'name="code"' in page.text


async def test_wrong_setup_code_rejected(client, code):
    r = await client.post("/admin/setup", data={"code": "AAAA-AAAA-AAAA"})
    assert r.status_code == 400 and "That code is not valid" in r.text
    assert "sb_setup" not in r.cookies
    r = await client.get("/admin/setup/telegram")
    assert r.headers["location"] == "/admin/setup"


async def test_setup_code_rate_limited(client, code):
    for _ in range(10):
        assert (await client.post("/admin/setup", data={"code": "WRONG"})).status_code == 400
    r = await client.post("/admin/setup", data={"code": code})
    assert r.status_code == 429


async def test_rate_limit_window_expires(client, code, frozen):
    for _ in range(10):
        await client.post("/admin/setup", data={"code": "WRONG"})
    frozen["now"] = NOW + timedelta(minutes=11)
    await enter_code(client, code)


async def test_correct_code_sets_cookie_and_unlocks_telegram(client, code):
    r = await enter_code(client, code)
    assert "sb_setup" in r.cookies
    page = await client.get("/admin/setup/telegram")
    assert page.status_code == 200 and 'name="token"' in page.text


async def test_tampered_setup_cookie_rejected(client, code):
    client.cookies.set("sb_setup", "forged")
    r = await client.get("/admin/setup/telegram")
    assert r.headers["location"] == "/admin/setup"


async def test_setup_cookie_expires_after_an_hour(client, code, frozen, monkeypatch):
    await enter_code(client, code)
    import time
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3601)
    r = await client.get("/admin/setup/telegram")
    assert r.headers["location"] == "/admin/setup"


async def test_wizard_telegram_step_validates_token(client, code, web_app, db, store):
    await enter_code(client, code)
    web_app.state.telegram_factory = lambda token: FailingTelegram()
    r = await client.post("/admin/setup/telegram", data={"token": "123:bad-token"})
    assert r.status_code == 400
    assert "Telegram rejected the token" in r.text and "RuntimeError" in r.text
    assert "123:bad-token" not in r.text
    assert await fresh(db, store, K.TG_TOKEN) is None
    assert await fresh(db, store, K.TG_BOT_USERNAME) is None


async def test_wizard_telegram_step_stores_token(client, code, db, store):
    await enter_code(client, code)
    r = await client.post("/admin/setup/telegram", data={"token": "123:good"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/setup/bind"
    assert await fresh(db, store, K.TG_TOKEN) == "123:good"
    assert await store.get(K.TG_BOT_USERNAME) == "test_bot"
    assert await store.get(K.PORTAL_BASE_URL) == "http://test"
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "admin.setup_telegram"))
    assert entry is not None and "123:good" not in str(entry.details)


async def test_bind_page_opens_window(client, code, db, store):
    await enter_code(client, code)
    await client.post("/admin/setup/telegram", data={"token": "123:good"})
    r = await client.get("/admin/setup/bind")
    assert r.status_code == 200
    assert "/bind" in r.text and "Remain anonymous" in r.text
    assert 'hx-get="/admin/setup/bind/status"' in r.text and "every 2s" in r.text
    until = await fresh(db, store, K.SETUP_BIND_OPEN_UNTIL)
    assert datetime.fromisoformat(until) == NOW + timedelta(minutes=15)


async def test_bind_status_completes_setup(client, code, db, store):
    await enter_code(client, code)
    await client.post("/admin/setup/telegram", data={"token": "123:good"})
    await client.get("/admin/setup/bind")
    waiting = await client.get("/admin/setup/bind/status")
    assert waiting.status_code == 200 and "Waiting" in waiting.text
    assert 'hx-trigger="every 2s"' in waiting.text
    # the bot's /bind handler stores these
    await store.set(K.TG_GROUP_ID, -100123)
    await store.set(K.TG_TOPIC_ID, 7)
    await store.set(K.TG_GROUP_TITLE, "Lab group")
    await db.commit()
    bound = await client.get("/admin/setup/bind/status")
    assert "Bound to “Lab group”" in bound.text
    assert "That's right — continue" in bound.text and "Wrong group — reset" in bound.text
    assert "every 2s" not in bound.text
    assert await fresh(db, store, K.SETUP_COMPLETED, False) is False  # only on confirm
    r = await client.post("/admin/setup/bind/confirm")
    assert r.status_code == 303 and r.headers["location"] == "/admin/setup/grafana"
    assert await fresh(db, store, K.SETUP_COMPLETED) is True


async def test_bind_reset_clears_group(client, code, db, store, frozen):
    await enter_code(client, code)
    await client.post("/admin/setup/telegram", data={"token": "123:good"})
    for k, v in ((K.TG_GROUP_ID, -1), (K.TG_TOPIC_ID, 3), (K.TG_GROUP_TITLE, "Wrong")):
        await store.set(k, v)
    await db.commit()
    frozen["now"] = NOW + timedelta(minutes=5)
    r = await client.post("/admin/setup/bind/reset")
    assert r.status_code == 303 and r.headers["location"] == "/admin/setup/bind"
    for k in (K.TG_GROUP_ID, K.TG_TOPIC_ID, K.TG_GROUP_TITLE):
        assert await fresh(db, store, k) is None
    until = await store.get(K.SETUP_BIND_OPEN_UNTIL)
    assert datetime.fromisoformat(until) == NOW + timedelta(minutes=20)
    assert await store.get(K.SETUP_COMPLETED, False) is False


async def test_confirm_requires_binding(client, code, db, store):
    await enter_code(client, code)
    r = await client.post("/admin/setup/bind/confirm")
    assert r.status_code == 303 and r.headers["location"] == "/admin/setup/bind"
    assert await fresh(db, store, K.SETUP_COMPLETED, False) is False


async def test_setup_code_unusable_after_completion(client, code, db, store):
    await enter_code(client, code)  # browser keeps a valid sb_setup cookie
    await setup.complete_setup(store)
    await db.commit()
    r = await client.get("/admin/setup/telegram")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = await client.post("/admin/setup", data={"code": code})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = await client.get("/admin")
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_admin_may_use_wizard_after_completion(client, db, store, web_app, make_user):
    await setup.complete_setup(store)
    admin = await make_user(display_name="Root", is_admin=True)
    sid = secrets.token_urlsafe(16)
    db.add(Session(id=sid, user_id=admin.id, created_at=NOW, expires_at=NOW + timedelta(days=1)))
    await db.commit()
    client.cookies.set("sb_session", web_app.state.signer.dumps(sid))
    assert (await client.get("/admin/setup/grafana")).status_code == 200
    assert (await client.get("/admin/setup/calendar")).status_code == 200


async def test_grafana_and_calendar_steps_skippable(client, code, db, store):
    await enter_code(client, code)
    r = await client.post("/admin/setup/grafana", data={"action": "skip"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/setup/calendar"
    r = await client.post("/admin/setup/calendar", data={"action": "skip"})
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert await fresh(db, store, K.GRAFANA_URL) is None


async def test_wizard_grafana_shows_default_queries_and_help(client, code):
    await enter_code(client, code)
    r = await client.get("/admin/setup/grafana")
    assert r.status_code == 200
    assert "node_cpu_seconds_total" in r.text and "DCGM_FI_DEV_GPU_UTIL" in r.text
    assert "{instance_re}" in r.text and "=~" in r.text
