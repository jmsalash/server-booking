import secrets
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

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
        self.series: dict[str, list[Series] | Exception] = {}  # needle -> labelled series
        self.args = None
        self.queries: list[str] = []

    async def list_datasources(self):
        if self.fail:
            raise self.fail
        return [Datasource("prom1", "Prometheus", "prometheus"),
                Datasource("loki1", "Loki", "loki")]

    async def instant_query(self, ds_uid, expr):
        self.queries.append(expr)
        if self.fail:
            raise self.fail
        for needle, out in self.series.items():
            if needle in expr:
                if isinstance(out, Exception):
                    raise out
                return out
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


# --- import from Grafana --------------------------------------------------------------------


async def _configure_grafana(db, store):
    await store.set(K.GRAFANA_URL, "http://grafana.lab")
    await store.set(K.GRAFANA_DS_UID, "prom1")
    await db.commit()


def _discovered(grafana, gpu=None):
    grafana.series["count by (instance) (node_cpu"] = [
        Series({"instance": "gpu-01.lab:9100"}, 64), Series({"instance": "cpu-02:9100"}, 8),
        Series({"instance": "old-03:9100"}, 4)]
    grafana.series["DCGM_FI_DEV_GPU_UTIL)"] = gpu if gpu is not None else [
        Series({"instance": "gpu-01:9835"}, 4)]


def _import_row(i, instance, name=None, gpu_count="0", selected=True, shareable=False):
    row = {f"instance_{i}": instance, f"name_{i}": instance if name is None else name,
           f"gpu_count_{i}": gpu_count}
    if selected:
        row[f"selected_{i}"] = "on"
    if shareable:
        row[f"shareable_{i}"] = "on"
    return row


async def test_servers_page_import_button_only_when_configured(client, logged_in_admin, db,
                                                               store, grafana):
    r = await client.get("/admin/servers")
    assert 'href="/admin/servers/import"' not in r.text
    assert "configure it on the" in r.text
    await _configure_grafana(db, store)
    r = await client.get("/admin/servers")
    assert 'href="/admin/servers/import"' in r.text
    assert grafana.queries == []  # plain page loads never talk to Grafana


async def test_import_page_lists_hosts_and_marks_existing(client, logged_in_admin, db, store,
                                                          make_server, grafana):
    await make_server(name="Legacy", kind="grafana", grafana_instance="OLD-03:9100")
    await make_server(name="CPU-02", kind="standalone")
    await _configure_grafana(db, store)
    _discovered(grafana)
    r = await client.get("/admin/servers/import")
    assert r.status_code == 200
    assert 'value="gpu-01.lab"' in r.text and 'value="cpu-02"' in r.text
    assert 'name="gpu_count_1" value="4"' in r.text  # sorted: cpu-02, gpu-01.lab, old-03
    assert r.text.count("Already added") == 2
    assert r.text.count("disabled") >= 2
    assert grafana.args[0] == "http://grafana.lab"


async def test_import_page_gpu_failure_note(client, logged_in_admin, db, store, grafana):
    await _configure_grafana(db, store)
    _discovered(grafana, gpu=GrafanaError("HTTPStatusError: 500"))
    r = await client.get("/admin/servers/import")
    assert r.status_code == 200 and 'value="gpu-01.lab"' in r.text
    assert "GPU count could not be read" in r.text


async def test_import_page_empty_discovery(client, logged_in_admin, db, store, grafana):
    await _configure_grafana(db, store)
    r = await client.get("/admin/servers/import")
    assert r.status_code == 200 and "No hosts found" in r.text


async def test_import_page_grafana_error_sanitized(client, logged_in_admin, db, store, grafana):
    await _configure_grafana(db, store)
    await store.set(K.GRAFANA_PASSWORD, "hunter2", secret=True)
    await db.commit()
    grafana.fail = GrafanaError("ConnectError: http://admin:hunter2@grafana.lab refused")
    r = await client.get("/admin/servers/import")
    assert r.status_code == 200
    assert "GrafanaError" in r.text and "refused" in r.text and "hunter2" not in r.text


async def test_import_page_not_configured_hint(client, logged_in_admin, grafana):
    r = await client.get("/admin/servers/import")
    assert r.status_code == 200 and "Grafana is not configured" in r.text
    assert 'name="instance_0"' not in r.text
    assert grafana.args is None and grafana.queries == []


async def test_import_creates_ticked_servers(client, logged_in_admin, db, store, grafana):
    form = {**_import_row(0, "cpu-02", "CPU two"),
            **_import_row(1, "gpu-01.lab", "gpu-01", "4", shareable=True),
            **_import_row(2, "old-03", selected=False)}
    r = await client.post("/admin/servers/import", data=form)
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/servers")
    db.expire_all()
    rows = {s.name: s for s in (await db.scalars(select(Server))).all()}
    assert set(rows) == {"CPU two", "gpu-01"}
    g = rows["gpu-01"]
    assert (g.kind, g.grafana_instance, g.hostname, g.has_gpu, g.gpu_count, g.shareable,
            g.enabled) == ("grafana", "gpu-01.lab", "gpu-01.lab", True, 4, True, True)
    c = rows["CPU two"]
    assert (c.has_gpu, c.gpu_count, c.shareable) == (False, 0, False)
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "admin.servers_import"))
    assert entry.details["names"] == ["CPU two", "gpu-01"]
    page = await client.get(r.headers["location"])
    assert "Added 2 servers" in page.text
    assert grafana.queries == []  # POST does not re-run discovery


async def test_import_invalid_row_creates_nothing(client, logged_in_admin, db, make_server):
    await make_server(name="taken")
    await db.commit()
    form = {**_import_row(0, "cpu-02"), **_import_row(1, "gpu-01", "gpu-01", "99"),
            **_import_row(2, "x-03", "TAKEN"), **_import_row(3, "y-04", "")}
    r = await client.post("/admin/servers/import", data=form)
    assert r.status_code == 400
    assert "from 1 to 64" in r.text and "already exists" in r.text and "Enter a name" in r.text
    assert 'value="cpu-02"' in r.text  # table re-rendered with the submitted rows
    db.expire_all()
    assert [s.name for s in (await db.scalars(select(Server))).all()] == ["taken"]


async def test_import_rejects_duplicates_within_batch_and_existing_instance(
        client, logged_in_admin, db, make_server):
    await make_server(name="legacy", kind="grafana", grafana_instance="old-03:9100")
    await db.commit()
    r = await client.post("/admin/servers/import", data={
        **_import_row(0, "a", "same"), **_import_row(1, "b", "SAME")})
    assert r.status_code == 400 and "already exists" in r.text
    r = await client.post("/admin/servers/import", data=_import_row(0, "old-03", "new-name"))
    assert r.status_code == 400 and "Already added" in r.text
    db.expire_all()
    assert len((await db.scalars(select(Server))).all()) == 1


async def test_import_nothing_ticked(client, logged_in_admin, db):
    r = await client.post("/admin/servers/import", data=_import_row(0, "a", selected=False))
    assert r.status_code == 400 and "Tick at least one" in r.text


async def test_import_forbidden_for_members(client, logged_in_member, grafana):
    assert (await client.get("/admin/servers/import")).status_code == 403
    r = await client.post("/admin/servers/import", data=_import_row(0, "a"))
    assert r.status_code == 403
    assert grafana.queries == []


async def test_integrations_page_shows_and_saves_discovery_queries(client, logged_in_admin, db,
                                                                   store, grafana):
    r = await client.get("/admin/integrations")
    assert 'name="q_discover_hosts"' in r.text and 'name="q_discover_gpus"' in r.text
    assert "count by (instance)" in r.text
    await client.post("/admin/integrations/grafana", data={
        "action": "save", "url": "http://grafana.lab", "user": "", "password": "", "token": "",
        "ds_uid": "", "q_discover_hosts": "count by (instance) (up)"})
    assert (await fresh(db, store, K.GRAFANA_QUERIES))["discover_hosts"] == (
        "count by (instance) (up)")


async def test_integrations_migrates_stored_legacy_dcgm_defaults(client, logged_in_admin, db,
                                                                 store, grafana):
    from app.integrations.grafana import DEFAULT_QUERIES, LEGACY_DEFAULT_QUERIES
    legacy = {k: v[0] for k, v in LEGACY_DEFAULT_QUERIES.items()}
    await store.set(K.GRAFANA_QUERIES, {**legacy, "cpu": "my_cpu"})
    await db.commit()
    r = await client.get("/admin/integrations")
    assert 'name="q_gpu_util" rows="3">DCGM_FI_DEV_GPU_UTIL{' in r.text
    assert 'name="q_discover_gpus" rows="3">count by (instance) (DCGM_FI_DEV_GPU_UTIL)' in r.text
    assert 'name="q_cpu" rows="3">my_cpu<' in r.text
    assert "nvidia_gpu_exporter" in r.text and "DCGM_FI_DEV_FB_FREE" in r.text  # help text
    # Re-saving the form with the old DCGM text does not store it as an override.
    await client.post("/admin/integrations/grafana", data={
        **GRAFANA_FORM, "q_cpu": "my_cpu", **{f"q_{k}": v for k, v in legacy.items() if k != "cpu"},
        "q_gpu_mem": DEFAULT_QUERIES["gpu_mem"]})
    assert await fresh(db, store, K.GRAFANA_QUERIES) == {"cpu": "my_cpu"}


# --- duplicate machines (hostname / instance / IP) ---------------------------------------


@pytest.fixture
def dns(web_app):
    table: dict[str, set[str]] = {}

    async def resolve(name):
        if name not in table:
            raise OSError(f"no such host {name}")
        return table[name]

    web_app.state.resolver = resolve
    return table


async def test_create_rejects_same_machine_by_name(client, logged_in_admin, db, make_server, dns):
    await make_server(name="gpu-01", hostname="gpu-01.lab")
    await db.commit()
    r = await client.post("/admin/servers/new", data={
        **SERVER_FORM, "hostname": "other.lab", "grafana_instance": "GPU-01.LAB:9100"})
    assert r.status_code == 400
    assert "This machine already exists as &#34;gpu-01&#34;." in r.text
    assert len((await db.scalars(select(Server))).all()) == 1


async def test_create_rejects_same_machine_by_ip(client, logged_in_admin, db, make_server, dns):
    await make_server(name="by-ip", hostname="10.0.0.5")
    await db.commit()
    dns["cpu-07.lab"] = {"10.0.0.5"}
    r = await client.post("/admin/servers/new", data=SERVER_FORM)
    assert r.status_code == 400 and "already exists as &#34;by-ip&#34;" in r.text
    dns.clear()  # DNS failure: only names are compared, the save goes through
    r = await client.post("/admin/servers/new", data=SERVER_FORM)
    assert r.status_code == 303


async def test_edit_not_flagged_against_itself_but_against_others(client, logged_in_admin, db,
                                                                  make_server, dns):
    s = await make_server(name="cpu-07", hostname="cpu-07.lab")
    await make_server(name="alias", hostname="alias.lab")
    await db.commit()
    dns.update({"cpu-07.lab": {"10.0.0.7"}, "alias.lab": {"10.0.0.8"}})
    r = await client.post(f"/admin/servers/{s.id}", data={**SERVER_FORM, "notes": "x"})
    assert r.status_code == 303
    dns["mon.lab"] = {"10.0.0.8"}  # a changed instance that is really "alias"
    r = await client.post(f"/admin/servers/{s.id}", data={**SERVER_FORM,
                                                          "grafana_instance": "mon.lab"})
    assert r.status_code == 400 and "already exists as &#34;alias&#34;" in r.text


async def test_edit_with_unchanged_keys_skips_ip_check(client, logged_in_admin, db,
                                                      make_server, dns):
    s = await make_server(name="cpu-07", hostname="cpu-07.lab")
    await make_server(name="alias", hostname="alias.lab")
    await db.commit()
    dns.update({"cpu-07.lab": {"10.0.0.7"}, "alias.lab": {"10.0.0.7"}})  # DNS changed later
    r = await client.post(f"/admin/servers/{s.id}", data={**SERVER_FORM, "notes": "toggle"})
    assert r.status_code == 303
    # a changed key is checked by IP again
    r = await client.post(f"/admin/servers/{s.id}", data={**SERVER_FORM, "grafana_instance":
                                                          "cpu-07.lab:9100", "notes": "z"})
    assert r.status_code == 303  # same normalised key set: still unchanged
    r = await client.post(f"/admin/servers/{s.id}", data={**SERVER_FORM, "hostname": "new.lab",
                                                          "grafana_instance": "cpu-07.lab"})
    assert r.status_code == 400 and "already exists as &#34;alias&#34;" in r.text


async def test_import_tampered_instance_with_port_is_duplicate(client, logged_in_admin, db,
                                                               make_server, dns):
    await make_server(name="legacy", kind="grafana", grafana_instance="old-03:9100")
    await db.commit()
    r = await client.post("/admin/servers/import", data=_import_row(0, "old-03:9100", "fresh"))
    assert r.status_code == 400 and "already exists as &#34;legacy&#34;" in r.text
    r = await client.post("/admin/servers/import", data={
        **_import_row(0, "a", "a1"), **_import_row(1, "a:9100", "a2")})
    assert r.status_code == 400 and "already exists as &#34;a1&#34;" in r.text
    db.expire_all()
    assert len((await db.scalars(select(Server))).all()) == 1


async def test_import_by_ip(client, logged_in_admin, db, store, make_server, grafana, dns):
    await make_server(name="by-ip", hostname="10.0.0.5")
    await _configure_grafana(db, store)
    _discovered(grafana)
    dns["cpu-02"] = {"10.0.0.5"}
    r = await client.get("/admin/servers/import")
    assert r.text.count("Already added") == 1  # cpu-02 is by-ip
    r = await client.post("/admin/servers/import", data=_import_row(0, "cpu-02"))
    assert r.status_code == 400 and "already exists as &#34;by-ip&#34;" in r.text
    assert "Already added" in r.text


async def test_import_page_caps_rows(client, logged_in_admin, db, store, grafana, monkeypatch):
    monkeypatch.setattr("app.web.admin.MAX_IMPORT_ROWS", 2)
    await _configure_grafana(db, store)
    _discovered(grafana)
    r = await client.get("/admin/servers/import")
    assert "Showing the first 2 hosts" in r.text
    assert 'name="instance_1"' in r.text and 'name="instance_2"' not in r.text


async def test_added_notice_is_clamped(client, logged_in_admin):
    r = await client.get("/admin/servers?added=999999999")
    assert "Added" not in r.text
    r = await client.get("/admin/servers?added=-3")
    assert "Added" not in r.text
    r = await client.get("/admin/servers?added=²")
    assert r.status_code == 200 and "Added" not in r.text
    r = await client.get("/admin/servers?added=1")
    assert "Added 1 server." in r.text


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


async def test_clearing_grafana_url_drops_credentials(client, logged_in_admin, db, store,
                                                      grafana):
    await store.set(K.GRAFANA_URL, "http://grafana.lab")
    await store.set(K.GRAFANA_PASSWORD, "old-pw", secret=True)
    await store.set(K.GRAFANA_TOKEN, "old-tok", secret=True)
    await db.commit()
    r = await client.post("/admin/integrations/grafana", data={**GRAFANA_FORM, "url": "",
                                                              "password": "typed-pw"})
    assert r.status_code == 303
    assert await fresh(db, store, K.GRAFANA_URL) is None
    assert await store.get(K.GRAFANA_PASSWORD) is None and await store.get(K.GRAFANA_TOKEN) is None
    entry = (await db.scalars(select(AuditLog).where(AuditLog.action == "admin.grafana")
                              .order_by(AuditLog.id.desc()))).first()
    assert entry.details["credentials_cleared"] is True
    r = await client.post("/admin/integrations/grafana", data={
        **GRAFANA_FORM, "action": "test", "url": "http://evil.example"})
    assert r.status_code == 200
    assert grafana.args == ("http://evil.example", "viewer", None, None)
    assert await fresh(db, store, K.GRAFANA_PASSWORD) is None
    assert await store.get(K.GRAFANA_TOKEN) is None


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


async def test_health_page_shows_worker_and_last_ok_poll(client, logged_in_admin, db, store):
    await store.set(K.WORKER_HEARTBEAT, {
        "at": (NOW - timedelta(seconds=30)).isoformat(), "bot_running": True,
        "bot_username": "lab_bot",
    })
    await store.set(HEALTH_KEY, {"at": NOW.isoformat(), "ok": 0, "failed": {},
                                 "last_ok_at": (NOW - timedelta(hours=1)).isoformat()})
    await db.commit()
    r = await client.get("/admin/health")
    assert "Worker last seen Thu 05 10:59 (bot running" in r.text
    assert "@lab_bot" in r.text
    assert "Last successful poll: Thu 05 10:00" in r.text
    assert "worker may be down" not in r.text


async def test_health_page_warns_when_worker_silent(client, logged_in_admin, db, store):
    await store.set(K.WORKER_HEARTBEAT, {
        "at": (NOW - timedelta(minutes=3)).isoformat(), "bot_running": False,
        "bot_username": None,
    })
    await db.commit()
    r = await client.get("/admin/health")
    assert "Worker last seen Thu 05 10:57 (bot not running)" in r.text
    assert "worker may be down" in r.text


async def test_health_page_without_heartbeat(client, logged_in_admin):
    r = await client.get("/admin/health")
    assert "The worker has not reported yet" in r.text


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


class _CheckboxLabels(HTMLParser):
    """Collects (input name, text of the enclosing <label>) for every checkbox."""

    def __init__(self):
        super().__init__()
        self.labels: list[dict] = []  # open <label> elements
        self.found: list[tuple[str, dict | None]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label":
            self.labels.append({"text": ""})
        elif tag == "input" and a.get("type") == "checkbox":
            self.found.append((a.get("name", ""), self.labels[-1] if self.labels else None))

    def handle_endtag(self, tag):
        if tag == "label" and self.labels:
            self.labels.pop()

    def handle_data(self, data):
        for label in self.labels:
            label["text"] += data


@pytest.mark.parametrize("path", ["/admin/servers/new", "edit"])
async def test_server_form_checkboxes_have_visible_labels(client, logged_in_admin, make_server,
                                                          db, path):
    if path == "edit":
        s = await make_server(name="gpu-09", kind="grafana", has_gpu=True, gpu_count=2)
        await db.commit()
        path = f"/admin/servers/{s.id}"
    parser = _CheckboxLabels()
    parser.feed((await client.get(path)).text)
    labels = {name: label for name, label in parser.found}
    assert set(labels) == {"has_gpu", "shareable", "enabled"}
    for name, label in labels.items():
        assert label is not None, f"checkbox {name} is not inside a <label>"
        assert label["text"].strip(), f"checkbox {name} has no visible label text"
    assert labels["shareable"]["text"].strip() == "Shareable"
    assert labels["enabled"]["text"].strip() == "Enabled"
    assert labels["has_gpu"]["text"].strip() == "Has GPU"


# --- delete (archive) / restore / delete permanently ----------------------------------------


async def _seed_history(db, make_user, server):
    """A past, a running and a future booking, plus a snapshot and a watch subscription."""
    from app.models import MetricSnapshot, WatchSubscription

    owner = await make_user(display_name="Bea")
    past = Booking(user_id=owner.id, server_id=server.id, start_at=NOW - timedelta(days=1),
                   end_at=NOW - timedelta(hours=20), resource="cpu", status="completed")
    running = Booking(user_id=owner.id, server_id=server.id, start_at=NOW - timedelta(hours=1),
                      end_at=NOW + timedelta(hours=2), resource="cpu", calendar_event_id="ev1")
    future = Booking(user_id=owner.id, server_id=server.id, start_at=NOW + timedelta(days=2),
                     end_at=NOW + timedelta(days=2, hours=3), resource="cpu",
                     calendar_event_id="ev2")
    db.add_all([past, running, future,
                MetricSnapshot(server_id=server.id, ts=NOW, cpu_pct=1.0, ram_pct=2.0),
                WatchSubscription(user_id=owner.id, server_id=server.id, created_at=NOW)])
    await db.flush()
    return owner, past, running, future


async def test_delete_server_confirm_page_counts_upcoming(client, logged_in_admin, db,
                                                          make_user, make_server):
    s = await make_server(name="gpu-05")
    await _seed_history(db, make_user, s)
    await db.commit()
    r = await client.get(f"/admin/servers/{s.id}")
    assert f'href="/admin/servers/{s.id}/delete"' in r.text
    r = await client.get(f"/admin/servers/{s.id}/delete")
    assert r.status_code == 200
    assert "2 upcoming or running bookings will be cancelled" in r.text
    assert f'action="/admin/servers/{s.id}/delete"' in r.text


async def test_delete_server_archives_and_cancels_future_bookings(
        client, logged_in_admin, db, store, make_user, make_server):
    from app.models import MetricSnapshot, WatchSubscription

    s = await make_server(name="gpu-05", kind="grafana")
    owner, past, running, future = await _seed_history(db, make_user, s)
    await store.set(K.GCAL_CALENDAR_ID, "cal@x")
    ids = (s.id, past.id, running.id, future.id)
    await db.commit()
    r = await client.post(f"/admin/servers/{s.id}/delete")
    assert r.status_code == 303 and r.headers["location"] == f"/admin/servers?deleted_id={s.id}"
    db.expire_all()
    server = await db.get(Server, ids[0])
    assert server.archived_at == NOW and server.enabled is False
    assert (await db.get(Booking, ids[1])).status == "completed"  # history untouched
    for bid in ids[2:]:
        b = await db.get(Booking, bid)
        assert b.status == "cancelled" and b.cancelled_at == NOW
    items = (await db.scalars(select(OutboxItem).order_by(OutboxItem.id))).all()
    deletes = sorted(i.payload["booking_id"] for i in items if i.kind == "calendar_delete")
    assert deletes == sorted(ids[2:])
    dms = [i.payload["text"] for i in items if i.kind == "telegram_message"]
    assert len(dms) == 2 and all("gpu-05" in t and "cancelled by an admin" in t for t in dms)
    assert (await db.scalars(select(WatchSubscription))).all() == []
    assert len((await db.scalars(select(MetricSnapshot))).all()) == 1
    assert "admin.server_delete" in await _actions(db)
    # hidden from the live lists
    page = await client.get("/admin/servers?deleted_id=%d" % ids[0])
    assert "Deleted gpu-05" in page.text and "Show deleted servers (1)" in page.text
    assert f'href="/admin/servers/{ids[0]}"' not in page.text
    board_page = await client.get("/")
    assert "gpu-05" not in board_page.text
    assert "gpu-05" not in (await client.get("/book")).text
    assert (await client.get(f"/admin/servers/{ids[0]}")).status_code == 404
    assert (await client.post(f"/admin/servers/{ids[0]}/delete")).status_code == 404
    assert (await client.post(f"/watch/{ids[0]}")).status_code == 404


async def test_deleted_server_cannot_be_booked(client, logged_in_admin, db, make_server):
    s = await make_server(name="gone", archived_at=NOW, enabled=True)
    await db.commit()
    r = await client.post("/book", data={"server_id": str(s.id), "start": "2026-11-06T10:00",
                                         "end": "2026-11-06T11:00", "resource": "cpu"})
    assert r.status_code == 400
    db.expire_all()
    assert (await db.scalars(select(Booking))).all() == []


async def test_name_reusable_after_delete(client, logged_in_admin, db, make_server):
    s = await make_server(name="cpu-07", hostname="old-07.lab")
    await db.commit()
    await client.post(f"/admin/servers/{s.id}/delete")
    r = await client.post("/admin/servers/new", data=SERVER_FORM)  # name cpu-07 again
    assert r.status_code == 303
    db.expire_all()
    names = (await db.scalars(select(Server.name).where(Server.archived_at.is_(None)))).all()
    assert names == ["cpu-07"]


async def test_deleted_servers_list_toggle(client, logged_in_admin, db, make_server):
    await make_server(name="live-1")
    gone = await make_server(name="gone-1", archived_at=NOW, enabled=False)
    await db.commit()
    r = await client.get("/admin/servers")
    assert "live-1" in r.text and "gone-1" not in r.text
    assert 'href="/admin/servers?deleted=1"' in r.text and "Show deleted servers (1)" in r.text
    r = await client.get("/admin/servers", params={"deleted": "1"})
    assert "gone-1" in r.text and "Deleted 5 Nov 2026" in r.text
    assert f'action="/admin/servers/{gone.id}/restore"' in r.text
    assert f'href="/admin/servers/{gone.id}/purge"' in r.text
    assert "Hide deleted servers" in r.text


async def test_restore_server(client, logged_in_admin, db, make_server):
    s = await make_server(name="gone-1", archived_at=NOW, enabled=False)
    sid = s.id
    await db.commit()
    r = await client.post(f"/admin/servers/{sid}/restore")
    assert r.status_code == 303 and r.headers["location"] == f"/admin/servers?restored_id={sid}"
    db.expire_all()
    s = await db.get(Server, sid)
    assert s.archived_at is None and s.enabled is False
    assert "admin.server_restore" in await _actions(db)
    assert "Restored gone-1" in (await client.get(r.headers["location"])).text
    # only deleted servers can be restored or purged
    assert (await client.post(f"/admin/servers/{sid}/restore")).status_code == 404
    assert (await client.get(f"/admin/servers/{sid}/purge")).status_code == 404
    assert (await client.post(f"/admin/servers/{sid}/purge")).status_code == 404


async def test_restore_refused_when_name_taken(client, logged_in_admin, db, make_server):
    gone = await make_server(name="cpu-07", hostname="a.lab", archived_at=NOW, enabled=False)
    await make_server(name="CPU-07", hostname="b.lab")
    gid = gone.id
    await db.commit()
    r = await client.post(f"/admin/servers/{gone.id}/restore")
    assert r.status_code == 409
    assert "Cannot restore cpu-07" in r.text and "already exists" in r.text
    db.expire_all()
    assert (await db.get(Server, gid)).archived_at == NOW


async def test_restore_refused_when_same_machine(client, logged_in_admin, db, make_server, dns):
    gone = await make_server(name="old", hostname="box.lab", archived_at=NOW, enabled=False)
    await make_server(name="new", hostname="box.lab")
    gid = gone.id
    await db.commit()
    r = await client.post(f"/admin/servers/{gone.id}/restore")
    assert r.status_code == 409 and "already exists as &#34;new&#34;" in r.text
    db.expire_all()
    assert (await db.get(Server, gid)).archived_at == NOW


async def test_deleted_server_not_a_duplicate(client, logged_in_admin, db, store, make_server,
                                              grafana, dns):
    await make_server(name="legacy", kind="grafana", grafana_instance="old-03:9100",
                      archived_at=NOW, enabled=False)
    await _configure_grafana(db, store)
    _discovered(grafana)
    r = await client.get("/admin/servers/import")
    assert "Already added" not in r.text
    r = await client.post("/admin/servers/new", data={**SERVER_FORM, "name": "legacy",
                                                      "hostname": "old-03"})
    assert r.status_code == 303


async def test_purge_server_removes_children(client, logged_in_admin, db, make_user,
                                             make_server):
    from app.models import MetricSnapshot, WatchSubscription

    s = await make_server(name="gpu-05")
    await _seed_history(db, make_user, s)
    other = await make_server(name="keep")
    db.add(MetricSnapshot(server_id=other.id, ts=NOW, cpu_pct=1.0, ram_pct=1.0))
    sid, other_id = s.id, other.id
    await db.commit()
    assert (await client.get(f"/admin/servers/{sid}/purge")).status_code == 404  # live
    await client.post(f"/admin/servers/{sid}/delete")
    r = await client.get(f"/admin/servers/{sid}/purge")
    assert r.status_code == 200
    assert "3 bookings" in r.text and "1 usage snapshot" in r.text
    assert "0 watchlist subscriptions" in r.text and "cannot be undone" in r.text
    r = await client.post(f"/admin/servers/{sid}/purge")
    assert r.status_code == 303 and "purged=1" in r.headers["location"]
    db.expire_all()
    assert await db.get(Server, sid) is None
    assert (await db.scalars(select(Booking).where(Booking.server_id == sid))).all() == []
    assert (await db.scalars(select(MetricSnapshot.server_id))).all() == [other_id]
    assert (await db.scalars(select(WatchSubscription))).all() == []
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "admin.server_purge"))
    assert entry.details["name"] == "gpu-05" and entry.details["bookings"] == 3
    assert "permanently deleted" in (await client.get(r.headers["location"])).text


async def test_purge_waits_for_pending_calendar_deletes(client, logged_in_admin, db, store,
                                                        make_user, make_server):
    s = await make_server(name="gpu-05")
    await _seed_history(db, make_user, s)
    await store.set(K.GCAL_CALENDAR_ID, "cal@x")
    sid = s.id
    await db.commit()
    await client.post(f"/admin/servers/{sid}/delete")  # queues calendar deletes
    r = await client.post(f"/admin/servers/{sid}/purge")
    assert r.status_code == 409 and "Calendar events are still being removed" in r.text
    db.expire_all()
    assert await db.get(Server, sid) is not None
    from sqlalchemy import update
    await db.execute(update(OutboxItem).values(done_at=NOW))
    await db.commit()
    assert (await client.post(f"/admin/servers/{sid}/purge")).status_code == 303


async def test_bookings_of_deleted_servers_hidden_by_default(client, logged_in_admin, db,
                                                             make_user, make_server):
    u = await make_user(display_name="Cleo")
    live = await make_server(name="live-1")
    gone = await make_server(name="gone-1", archived_at=NOW, enabled=False)
    for s in (live, gone):
        db.add(Booking(user_id=u.id, server_id=s.id, start_at=NOW - timedelta(days=2),
                       end_at=NOW - timedelta(days=2) + timedelta(hours=1), resource="cpu",
                       status="completed"))
    await db.commit()
    r = await client.get("/admin/bookings")
    assert "live-1" in r.text and "gone-1" not in r.text
    assert 'name="deleted"' in r.text and "Include deleted servers" in r.text
    r = await client.get("/admin/bookings", params={"deleted": "1"})
    assert "gone-1" in r.text and '<span class="badge badge--danger">deleted</span>' in r.text
    assert "gone-1 (deleted)" in r.text  # in the server filter
