import secrets
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

import app.clock
from app.domain.rules import Rules
from app.integrations.grafana import Datasource, GrafanaError, Series
from app.jobs.metrics import HEALTH_KEY
from app.models import AuditLog, Booking, OutboxItem, Server, Session, TelegramTopic
from app.services import setup
from app.settings_store import K, SettingsStore
from tests.fakes import FakeCalendar, FakeTelegram

NOW = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)  # before the `booking` fixture (Nov 6 09:00)


@pytest_asyncio.fixture
async def store(db, web_app):
    """Settings written by the test must be readable by the app (same encryption key)."""
    return SettingsStore(db, web_app.state.box)


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(app.clock, "utcnow", lambda: state["now"])
    return state


class FakeGrafana:
    def __init__(self, fail: Exception | None = None, values: dict | None = None):
        self.fail = fail
        self.values = values or {}
        self.args = None

    async def list_datasources(self):
        if self.fail:
            raise self.fail
        return [Datasource("prom1", "Prometheus", "prometheus"),
                Datasource("loki1", "Loki", "loki")]

    async def instant_query(self, ds_uid, expr):
        if self.fail:
            raise self.fail
        for needle, v in self.values.items():
            if needle in expr:
                return [Series({}, v)]
        return []


@pytest.fixture
def grafana(web_app):
    fake = FakeGrafana(values={"node_cpu": 12.5, "MemAvailable": 40.0})

    def factory(url, user, password, token):
        fake.args = (url, user, password, token)
        return fake

    web_app.state.grafana_factory = factory
    return fake


@pytest.fixture
def calendar(web_app):
    fake = FakeCalendar()
    web_app.state.calendar_factory = lambda sa_json, cal_id: fake
    return fake


async def _login(db, client, web_app, user):
    sid = secrets.token_urlsafe(16)
    db.add(Session(id=sid, user_id=user.id, created_at=NOW, expires_at=NOW + timedelta(days=30)))
    await db.commit()
    client.cookies.set("sb_session", web_app.state.signer.dumps(sid))


@pytest_asyncio.fixture
async def completed(db, store):
    await setup.complete_setup(store)
    await db.commit()


@pytest_asyncio.fixture
async def logged_in_admin(db, client, web_app, make_user, completed):
    user = await make_user(display_name="Root", is_admin=True)
    await _login(db, client, web_app, user)
    return user


@pytest_asyncio.fixture
async def logged_in_member(db, client, web_app, make_user, completed):
    user = await make_user(display_name="Ana")
    await _login(db, client, web_app, user)
    return user


async def fresh(db, store, key, default=None):
    db.expire_all()
    return await store.get(key, default)


async def _actions(db) -> list[str]:
    db.expire_all()
    return list((await db.scalars(select(AuditLog.action).order_by(AuditLog.id))).all())


SERVER_FORM = {"name": "cpu-07", "hostname": "cpu-07.lab", "kind": "grafana",
               "grafana_instance": "", "gpu_count": "0", "enabled": "on", "notes": ""}


# --- access ---------------------------------------------------------------------------------


async def test_non_admin_forbidden(client, logged_in_member):
    assert (await client.get("/admin/servers")).status_code == 403
    assert (await client.post("/admin/rules", data={})).status_code == 403


async def test_admin_requires_login_after_setup(client, completed):
    r = await client.get("/admin/servers")
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_admin_nav_only_for_admins(client, logged_in_admin):
    r = await client.get("/admin/servers")
    assert r.status_code == 200
    for href in ("/admin/integrations", "/admin/rules", "/admin/bookings", "/admin/health",
                 "/admin/audit"):
        assert f'href="{href}"' in r.text


async def test_member_board_has_no_admin_link(client, logged_in_member):
    r = await client.get("/")
    assert 'href="/admin"' not in r.text


# --- servers --------------------------------------------------------------------------------


async def test_add_server(client, logged_in_admin, db):
    r = await client.post("/admin/servers/new", data={**SERVER_FORM, "has_gpu": "on",
                                                      "gpu_count": "2", "shareable": "on"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/servers"
    s = await db.scalar(select(Server).where(Server.name == "cpu-07"))
    assert (s.kind, s.has_gpu, s.gpu_count, s.shareable, s.enabled, s.grafana_instance) == (
        "grafana", True, 2, True, True, None)


async def test_standalone_server_cannot_be_shareable(client, logged_in_admin, db):
    r = await client.post("/admin/servers/new", data={**SERVER_FORM, "kind": "standalone",
                                                      "shareable": "on"})
    assert r.status_code == 303
    s = await db.scalar(select(Server).where(Server.name == "cpu-07"))
    assert s.shareable is False
    page = await client.get(f"/admin/servers/{s.id}")
    assert 'name="shareable"' in page.text and "disabled" in page.text


async def test_gpu_count_zero_without_gpu(client, logged_in_admin, db):
    await client.post("/admin/servers/new", data={**SERVER_FORM, "gpu_count": "4"})
    s = await db.scalar(select(Server).where(Server.name == "cpu-07"))
    assert s.has_gpu is False and s.gpu_count == 0


async def test_server_form_validation(client, logged_in_admin, db, make_server):
    await make_server(name="GPU-01")
    await db.commit()
    r = await client.post("/admin/servers/new", data={**SERVER_FORM, "name": "gpu-01"})
    assert r.status_code == 400 and "already exists" in r.text
    r = await client.post("/admin/servers/new", data={**SERVER_FORM, "has_gpu": "on",
                                                      "gpu_count": "-1"})
    assert r.status_code == 400 and "GPU count" in r.text
    r = await client.post("/admin/servers/new", data={**SERVER_FORM, "kind": "vm"})
    assert r.status_code == 400
    r = await client.post("/admin/servers/new", data={**SERVER_FORM, "hostname": ""})
    assert r.status_code == 400
    db.expire_all()
    assert len((await db.scalars(select(Server))).all()) == 1


async def test_gpu_server_needs_at_least_one_gpu(client, logged_in_admin, db):
    r = await client.post("/admin/servers/new", data={**SERVER_FORM, "has_gpu": "on",
                                                      "gpu_count": "0"})
    assert r.status_code == 400 and "from 1 to 64" in r.text
    r = await client.post("/admin/servers/new", data={**SERVER_FORM, "has_gpu": "on",
                                                      "gpu_count": "65"})
    assert r.status_code == 400
    assert (await db.scalars(select(Server))).all() == []


async def test_edit_server_keeps_own_name(client, logged_in_admin, db, make_server):
    s = await make_server(name="cpu-07")
    await db.commit()
    r = await client.post(f"/admin/servers/{s.id}", data={**SERVER_FORM, "notes": "rack 3"})
    assert r.status_code == 303
    sid = s.id
    db.expire_all()
    assert (await db.get(Server, sid)).notes == "rack 3"


async def test_disable_server_warns_about_remaining_bookings(client, logged_in_admin, booking, db):
    server = await db.get(Server, booking.server_id)
    sid, bid = server.id, booking.id
    await db.commit()
    form = {**SERVER_FORM, "name": server.name, "hostname": server.hostname,
            "kind": "standalone", "has_gpu": "on", "gpu_count": "1"}
    form.pop("enabled")
    r = await client.post(f"/admin/servers/{server.id}", data=form)
    assert r.status_code == 200
    assert "1 booking remains on this server" in r.text
    db.expire_all()
    assert (await db.get(Server, sid)).enabled is False
    assert (await db.get(Booking, bid)).status == "active"


async def test_fetch_usage_now(client, logged_in_admin, db, store, make_server, grafana):
    s = await make_server(name="mon-1", kind="grafana", grafana_instance="mon-1:9100")
    await store.set(K.GRAFANA_URL, "http://grafana.lab")
    await store.set(K.GRAFANA_DS_UID, "prom1")
    await db.commit()
    r = await client.post(f"/admin/servers/{s.id}/fetch")
    assert r.status_code == 200 and "12.5" in r.text and "40.0" in r.text
    assert "admin.fetch_usage" in await _actions(db)


async def test_fetch_usage_error_sanitized(client, logged_in_admin, db, store, make_server,
                                           grafana):
    s = await make_server(name="mon-1", kind="grafana")
    await store.set(K.GRAFANA_URL, "http://grafana.lab")
    await store.set(K.GRAFANA_DS_UID, "prom1")
    await store.set(K.GRAFANA_PASSWORD, "hunter2", secret=True)
    await db.commit()
    grafana.fail = GrafanaError("ConnectError: http://admin:hunter2@grafana.lab refused")
    r = await client.post(f"/admin/servers/{s.id}/fetch")
    assert "GrafanaError" in r.text and "refused" in r.text
    assert "hunter2" not in r.text


# --- rules ----------------------------------------------------------------------------------


def _rules_form(**over):
    r = Rules()
    form = {k: str(v) for k, v in r.to_dict().items() if k != "work_days"}
    form.update(over)
    return {**form, "work_days": [str(d) for d in r.work_days]}


async def test_rules_validation(client, logged_in_admin, db, store):
    r = await client.post("/admin/rules", data=_rules_form(work_start="19:00", work_end="09:00"))
    assert r.status_code == 400 and "before" in r.text
    r = await client.post("/admin/rules", data=_rules_form(timezone="Mars/Olympus",
                                                           max_booking_hours="0",
                                                           idle_cpu_pct="150",
                                                           work_start="9am"))
    assert r.status_code == 400
    for msg in ("time zone", "positive", "between 0 and 100", "HH:MM"):
        assert msg in r.text
    bad_day = _rules_form()
    bad_day["work_days"] = ["7"]
    assert (await client.post("/admin/rules", data=bad_day)).status_code == 400
    assert await fresh(db, store, K.RULES) is None


async def test_rules_upper_bounds(client, logged_in_admin, db, store):
    r = await client.post("/admin/rules", data=_rules_form(stale_after_min="2000000000"))
    assert r.status_code == 400 and "at most 10080" in r.text
    r = await client.post("/admin/rules", data=_rules_form(snapshot_retention_h="721",
                                                           max_idle_warnings="21"))
    assert r.status_code == 400 and "at most 720" in r.text and "at most 20" in r.text
    assert await fresh(db, store, K.RULES) is None


async def test_rules_saved(client, logged_in_admin, db, store):
    form = _rules_form(max_booking_hours="48", timezone="Europe/London", idle_cpu_pct="7.5")
    form["work_days"] = ["0", "2"]
    r = await client.post("/admin/rules", data=form)
    assert r.status_code == 303
    db.expire_all()
    rules = await store.get_rules()
    assert (rules.max_booking_hours, rules.timezone, rules.idle_cpu_pct, rules.work_days) == (
        48, "Europe/London", 7.5, (0, 2))
    assert "admin.rules" in await _actions(db)


# --- integrations ---------------------------------------------------------------------------


async def test_secret_field_blank_keeps_value(client, logged_in_admin, store, db, grafana):
    await store.set(K.GRAFANA_PASSWORD, "s3cret-pw", secret=True)
    await store.set(K.TG_TOKEN, "123:tok-secret", secret=True)
    await db.commit()
    page = await client.get("/admin/integrations")
    assert "s3cret-pw" not in page.text and "tok-secret" not in page.text
    assert "••••" in page.text
    r = await client.post("/admin/integrations/grafana", data={
        "action": "save", "url": "http://grafana.lab", "user": "viewer", "password": "",
        "token": "", "ds_uid": ""})
    assert r.status_code == 303
    assert await fresh(db, store, K.GRAFANA_PASSWORD) == "s3cret-pw"
    assert await store.get(K.GRAFANA_USER) == "viewer"
    r = await client.post("/admin/integrations/grafana", data={
        "action": "save", "url": "http://grafana.lab", "user": "viewer", "password": "new-pw",
        "token": "", "ds_uid": ""})
    assert await fresh(db, store, K.GRAFANA_PASSWORD) == "new-pw"
    entries = (await db.scalars(select(AuditLog))).all()
    assert all("new-pw" not in str(e.details) and "s3cret" not in str(e.details) for e in entries)


async def test_grafana_test_lists_prometheus_datasources(client, logged_in_admin, store, db,
                                                         grafana):
    await store.set(K.GRAFANA_PASSWORD, "pw", secret=True)
    await db.commit()
    r = await client.post("/admin/integrations/grafana", data={
        "action": "test", "url": "http://grafana.lab", "user": "viewer", "password": "",
        "token": "", "ds_uid": ""})
    assert r.status_code == 200 and "Connection OK" in r.text
    assert grafana.args == ("http://grafana.lab", "viewer", "pw", None)
    assert 'value="prom1"' in r.text and 'value="loki1"' not in r.text
    r = await client.post("/admin/integrations/grafana", data={
        "action": "save", "url": "http://grafana.lab", "user": "viewer", "password": "",
        "token": "", "ds_uid": "prom1", "q_cpu": "my_cpu{instance=\"{instance}\"}"})
    assert r.status_code == 303
    assert await fresh(db, store, K.GRAFANA_DS_UID) == "prom1"
    assert await store.get(K.GRAFANA_QUERIES) == {"cpu": 'my_cpu{instance="{instance}"}'}


GRAFANA_FORM = {"action": "save", "url": "http://grafana.lab", "user": "viewer", "password": "",
                "token": "", "ds_uid": ""}


async def test_grafana_url_with_credentials_rejected(client, logged_in_admin, db, store, grafana):
    r = await client.post("/admin/integrations/grafana", data={
        **GRAFANA_FORM, "url": "https://admin:pw123@grafana.lab"})
    assert r.status_code == 400 and "must not contain credentials" in r.text
    assert "pw123" not in r.text
    assert await fresh(db, store, K.GRAFANA_URL) is None
    # a URL stored before this check existed is never echoed with its credentials
    await store.set(K.GRAFANA_URL, "https://admin:pw123@grafana.lab")
    await db.commit()
    assert "pw123" not in (await client.get("/admin/integrations")).text


async def test_grafana_host_change_requires_secrets_again(client, logged_in_admin, db, store,
                                                          grafana):
    await store.set(K.GRAFANA_URL, "http://grafana.lab")
    await store.set(K.GRAFANA_PASSWORD, "old-pw", secret=True)
    await db.commit()
    r = await client.post("/admin/integrations/grafana", data={
        **GRAFANA_FORM, "action": "test", "url": "http://evil.example"})
    assert r.status_code == 400 and "re-enter the password" in r.text
    assert grafana.args is None  # nothing was sent anywhere
    assert await fresh(db, store, K.GRAFANA_URL) == "http://grafana.lab"
    # same host, different path: blank still keeps the saved password
    r = await client.post("/admin/integrations/grafana", data={
        **GRAFANA_FORM, "action": "test", "url": "http://grafana.lab/grafana"})
    assert r.status_code == 200 and grafana.args[2] == "old-pw"
    r = await client.post("/admin/integrations/grafana", data={
        **GRAFANA_FORM, "action": "test", "url": "http://new.lab", "password": "new-pw"})
    assert r.status_code == 200 and grafana.args == ("http://new.lab", "viewer", "new-pw", None)


async def test_grafana_test_failure_shown(client, logged_in_admin, grafana):
    grafana.fail = GrafanaError("HTTPStatusError: 401")
    r = await client.post("/admin/integrations/grafana", data={
        "action": "test", "url": "http://grafana.lab", "user": "", "password": "",
        "token": "", "ds_uid": ""})
    assert r.status_code == 400 and "GrafanaError" in r.text and "401" in r.text


async def test_calendar_upload_and_test(client, logged_in_admin, store, db, calendar):
    sa = '{"type": "service_account", "client_email": "x@y.iam", "private_key": "KEYDATA"}'
    r = await client.post("/admin/integrations/calendar",
                          data={"action": "test", "calendar_id": "lab@group.calendar"},
                          files={"sa_json": ("sa.json", sa, "application/json")})
    assert r.status_code == 200 and "Calendar test OK" in r.text and "KEYDATA" not in r.text
    assert await fresh(db, store, K.GCAL_SA_JSON) == sa
    assert await store.get(K.GCAL_CALENDAR_ID) == "lab@group.calendar"
    calendar.fail = True
    r = await client.post("/admin/integrations/calendar",
                          data={"action": "test", "calendar_id": "lab@group.calendar"},
                          files={"sa_json": ("", b"", "application/octet-stream")})
    assert r.status_code == 400 and "calendar down" in r.text
    assert await fresh(db, store, K.GCAL_SA_JSON) == sa  # blank upload keeps the value


async def test_calendar_rejects_invalid_json(client, logged_in_admin, store, db, calendar):
    r = await client.post("/admin/integrations/calendar",
                          data={"action": "save", "calendar_id": "c"},
                          files={"sa_json": ("sa.json", "not json", "application/json")})
    assert r.status_code == 400
    assert await fresh(db, store, K.GCAL_SA_JSON) is None


async def test_topic_dropdown_lists_discovered_topics(client, logged_in_admin, db, store):
    db.add(TelegramTopic(thread_id=11, title="General", last_seen_at=NOW - timedelta(days=2)))
    db.add(TelegramTopic(thread_id=22, title="Machine booking", last_seen_at=NOW))
    await db.commit()
    r = await client.get("/admin/integrations")
    assert "Machine booking (22)" in r.text and "General (11)" in r.text
    assert r.text.index("Machine booking (22)") < r.text.index("General (11)")
    r = await client.post("/admin/integrations/telegram", data={
        "token": "", "topic_id": "11", "post_new_booking": "on", "post_freed": "on"})
    assert r.status_code == 303
    assert await fresh(db, store, K.TG_TOPIC_ID) == 11
    assert await store.get(K.TG_TOPIC_POSTS) == {
        "new_booking": True, "reclaimed": False, "freed": True}
    r = await client.post("/admin/integrations/telegram", data={"token": "", "topic_id": "99"})
    assert r.status_code == 400
    assert await fresh(db, store, K.TG_TOPIC_ID) == 11


async def test_telegram_token_change_validated(client, logged_in_admin, db, store, web_app):
    web_app.state.telegram_factory = lambda token: FakeTelegram()
    r = await client.post("/admin/integrations/telegram", data={"token": "999:new"})
    assert r.status_code == 303
    assert await fresh(db, store, K.TG_TOKEN) == "999:new"
    assert await store.get(K.TG_BOT_USERNAME) == "test_bot"


async def test_portal_base_url(client, logged_in_admin, db, store):
    r = await client.post("/admin/integrations/portal", data={"base_url": "https://book.lab/"})
    assert r.status_code == 303
    assert await fresh(db, store, K.PORTAL_BASE_URL) == "https://book.lab"
    r = await client.post("/admin/integrations/portal", data={"base_url": "ftp://x"})
    assert r.status_code == 400


# --- bookings -------------------------------------------------------------------------------


async def test_bookings_filter_and_admin_cancel(client, logged_in_admin, booking, db):
    bid = booking.id
    await db.commit()
    r = await client.get("/admin/bookings", params={"status": "active"})
    assert r.status_code == 200 and "Ana Rossi" in r.text and "gpu-01" in r.text
    assert f'action="/admin/bookings/{bid}/cancel"' in r.text
    r = await client.get("/admin/bookings", params={"status": "cancelled"})
    assert "No bookings match" in r.text
    r = await client.post(f"/admin/bookings/{booking.id}/cancel", data={"reason": "maintenance"})
    assert r.status_code == 303
    db.expire_all()
    b = await db.get(Booking, bid)
    assert (b.status, b.cancel_reason) == ("cancelled", "maintenance")
    actions = await _actions(db)
    assert "booking.cancel" in actions and "admin.booking_cancel" in actions


# --- health / audit -------------------------------------------------------------------------


async def test_health_page(client, logged_in_admin, db, store, make_user, make_server):
    await store.set(HEALTH_KEY, {"at": NOW.isoformat(), "ok": 3, "failed": {"mon-9": "boom"}})
    await make_server(name="stale-1", kind="grafana")
    await make_user(display_name="Blocked Bob", dm_blocked=True)
    db.add(OutboxItem(kind="tg_dm", payload={}, created_at=NOW, next_attempt_at=NOW))
    await db.commit()
    r = await client.get("/admin/health")
    assert r.status_code == 200
    for text in ("mon-9", "boom", "stale-1", "Blocked Bob", "tg_dm", "not configured"):
        assert text in r.text


async def test_health_page_sanitises_poll_errors(client, logged_in_admin, db, store):
    await store.set(K.GRAFANA_TOKEN, "tok-123", secret=True)
    await store.set(HEALTH_KEY, {"at": NOW.isoformat(), "ok": 0, "failed": {
        "mon-1": "ConnectError: http://admin:pw9@grafana.lab refused, auth tok-123"}})
    await db.commit()
    r = await client.get("/admin/health")
    assert "refused" in r.text and "pw9" not in r.text and "tok-123" not in r.text


async def test_health_retry_resets_failed_outbox(client, logged_in_admin, db):
    old = NOW - timedelta(hours=30)
    item = OutboxItem(kind="gcal_create", payload={}, created_at=old, attempts=9,
                      next_attempt_at=old, failed_at=NOW - timedelta(hours=6), last_error="x")
    other = OutboxItem(kind="tg_dm", payload={}, created_at=old, attempts=2,
                       next_attempt_at=old, failed_at=old)
    db.add_all([item, other])
    await db.commit()
    item_id, other_id = item.id, other.id
    page = await client.get("/admin/health")
    assert "gcal_create" in page.text and "Retry now" in page.text
    r = await client.post("/admin/health/retry", data={"kind": "gcal_create"})
    assert r.status_code == 303
    db.expire_all()
    item = await db.get(OutboxItem, item_id)
    assert (item.failed_at, item.next_attempt_at, item.attempts) == (None, NOW, 9)
    assert (await db.get(OutboxItem, other_id)).failed_at is not None
    assert "admin.outbox_retry" in await _actions(db)


async def test_admin_actions_audited(client, logged_in_admin, db, make_server, booking):
    bid, admin_id = booking.id, logged_in_admin.id
    await db.commit()
    await client.post("/admin/servers/new", data=SERVER_FORM)
    s = await db.scalar(select(Server).where(Server.name == "cpu-07"))
    await client.post(f"/admin/servers/{s.id}", data={**SERVER_FORM, "notes": "x"})
    await client.post("/admin/rules", data=_rules_form(max_booking_hours="24"))
    await client.post("/admin/integrations/portal", data={"base_url": "https://book.lab"})
    await client.post(f"/admin/bookings/{bid}/cancel", data={"reason": ""})
    await client.post("/admin/health/retry", data={"kind": "tg_dm"})
    actions = await _actions(db)
    for a in ("admin.server_create", "admin.server_update", "admin.rules", "admin.portal",
              "admin.booking_cancel", "admin.outbox_retry"):
        assert a in actions
    entries = (await db.scalars(select(AuditLog).where(AuditLog.action.like("admin.%")))).all()
    assert all(e.actor_user_id == admin_id for e in entries)
    page = await client.get("/admin/audit")
    assert page.status_code == 200 and "admin.server_create" in page.text
