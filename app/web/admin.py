import asyncio
import json
import re
from collections.abc import Iterable
from dataclasses import fields as dc_fields
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import clock
from app.domain.rules import Rules
from app.integrations.gcal import GoogleCalendarClient
from app.integrations.grafana import (
    DEFAULT_QUERIES, NVIDIA_SMI_QUERIES, GrafanaClient, discover_hosts, effective_queries,
    fetch_usage, is_legacy_default, redact,
)
from app.jobs.metrics import HEALTH_KEY
from app.models import AuditLog, Booking, OutboxItem, Server, TelegramTopic, User
from app.services import audit, board, booking, inventory, server_archive
from app.services import usage as usage_svc
from app.services.machine_identity import MachineIndex, machine_keys
from app.settings_store import K, SettingsStore
from app.web.deps import current_user, get_db, get_store, require_admin, templates
from app.web.portal import check_id

router = APIRouter(prefix="/admin")

SECRET_PLACEHOLDER = "••••"
DS_CACHE_KEY = "grafana.datasources"  # Prometheus datasources from the last successful test
TOPIC_POSTS = (
    ("new_booking", "New bookings"),
    ("reclaimed", "Reclaimed bookings"),
    ("freed", "Servers freed"),
)
QUERY_KEYS = ("cpu", "ram", "gpu_util", "gpu_mem", "disk", "discover_hosts", "discover_gpus")
QUERY_HELP = {
    "disk": "Disk used % (per mount point): one series per mountpoint label. Optional — if it "
            "fails or returns nothing, readings just have no disk data.",
    "discover_hosts": "Import from Grafana: one series per host, labelled instance (a :port "
                      "suffix is dropped).",
    "discover_gpus": "Import from Grafana: GPU count per host, labelled instance (a :port "
                     "suffix is dropped); matched to the host case-insensitively, short name "
                     "or FQDN.",
}
MAX_IMPORT_ROWS = 1000
STATUSES = ("active", "cancelled", "reclaimed", "completed")
KINDS = ("grafana", "standalone")
LIST_LIMIT = 200
MAX_ID = 2**31 - 1
HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


class SetupRequired(Exception):
    """Setup is not completed; main.py redirects to /admin/setup."""


# --- factories (tests override them on app.state) -------------------------------------------


def default_grafana_factory(url, user, password, token) -> GrafanaClient:
    return GrafanaClient(url, user, password, token)


def default_calendar_factory(sa_json: str, calendar_id: str) -> GoogleCalendarClient:
    return GoogleCalendarClient(sa_json, calendar_id)


# --- helpers --------------------------------------------------------------------------------


def sanitize(message: str, secrets: Iterable[str | None] = ()) -> str:
    """Remove known secret values and URL credentials from an error message."""
    return redact(message, secrets)


def describe_error(e: Exception, secrets: Iterable[str | None] = ()) -> str:
    return f"{type(e).__name__}: {sanitize(str(e), secrets)}"


def valid_http_url(url: str) -> bool:
    """An http(s) URL with a host and no embedded credentials (user:pw@)."""
    parts = urlsplit(url)
    return (parts.scheme in ("http", "https") and bool(parts.hostname)
            and "@" not in parts.netloc)


def _host(url: str | None) -> str:
    return urlsplit(url).netloc.lower() if url else ""


async def record(
    db: AsyncSession, user: User | None, what: str, target: str, details: dict | None = None
) -> None:
    await audit.record(db, f"admin.{what}", target, now=clock.utcnow(), details=details,
                       actor_user_id=user.id if user else None)


async def admin_gate(
    request: Request, db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store)
) -> User:
    if not await store.get(K.SETUP_COMPLETED, False):
        raise SetupRequired()
    return await require_admin(await current_user(request, db))


def render(request: Request, name: str, ctx: dict, status: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _opt_int(value) -> int | None:
    try:
        n = int(str(value))
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= MAX_ID else None


# --- Grafana / Calendar forms (shared with the setup wizard) --------------------------------


async def grafana_ctx(store: SettingsStore) -> dict:
    queries = effective_queries(await store.get(K.GRAFANA_QUERIES))
    ds_uid = await store.get(K.GRAFANA_DS_UID)
    datasources = list(await store.get(DS_CACHE_KEY) or [])
    if ds_uid and all(d["uid"] != ds_uid for d in datasources):
        datasources.append({"uid": ds_uid, "name": ds_uid})
    return {
        "url": sanitize(await store.get(K.GRAFANA_URL) or ""),  # legacy user:pw@ URLs
        "user": await store.get(K.GRAFANA_USER) or "",
        "password_set": await store.get(K.GRAFANA_PASSWORD) is not None,
        "token_set": await store.get(K.GRAFANA_TOKEN) is not None,
        "ds_uid": ds_uid or "",
        "datasources": datasources,
        "queries": {k: queries[k] for k in QUERY_KEYS},
        "query_help": QUERY_HELP,
        "alt_gpu_queries": NVIDIA_SMI_QUERIES,
    }


async def calendar_ctx(store: SettingsStore) -> dict:
    return {
        "key_set": await store.get(K.GCAL_SA_JSON) is not None,
        "calendar_id": await store.get(K.GCAL_CALENDAR_ID) or "",
    }


async def _set_or_delete(store: SettingsStore, key: str, value) -> None:
    if value in (None, "", {}):
        await store.delete(key)
    else:
        await store.set(key, value)


async def handle_grafana_post(
    request: Request, db: AsyncSession, store: SettingsStore, user: User | None
) -> tuple[str | None, str | None, bool]:
    """Save the Grafana form; with action=test also run list_datasources.

    Returns (error, message, tested). Nothing is saved when the form is invalid.
    """
    form = await request.form()
    url = str(form.get("url", "")).strip()
    username = str(form.get("user", "")).strip()
    new_password = str(form.get("password", ""))
    new_token = str(form.get("token", "")).strip()
    ds_uid = str(form.get("ds_uid", "")).strip()
    queries = {}
    for k in QUERY_KEYS:
        q = str(form.get(f"q_{k}", "")).replace("\r\n", "\n").strip()
        if q and q != DEFAULT_QUERIES[k] and not is_legacy_default(k, q):
            queries[k] = q
    if url and not valid_http_url(url):
        return ("The Grafana URL must start with http:// or https:// and must not contain "
                "credentials (use the user/password fields).", None, False)
    old_url = await store.get(K.GRAFANA_URL)
    if old_url and url and _host(url) != _host(old_url):
        # Saved credentials are never sent to a different host: they must be typed again.
        missing = [label for key, label, new in (
            (K.GRAFANA_PASSWORD, "password", new_password),
            (K.GRAFANA_TOKEN, "API token", new_token),
        ) if not new and await store.get(key) is not None]
        if missing:
            return (f"The Grafana host changed — re-enter the {' and '.join(missing)}; saved "
                    "credentials are not sent to a new host.", None, False)
    allowed = {d["uid"] for d in await store.get(DS_CACHE_KEY) or []}
    if ds_uid and ds_uid not in allowed and ds_uid != await store.get(K.GRAFANA_DS_UID):
        return "Choose a datasource from the last connection test.", None, False

    await _set_or_delete(store, K.GRAFANA_URL, url)
    await _set_or_delete(store, K.GRAFANA_USER, username)
    cleared = False
    if not url:
        # Credentials are only kept together with the host they belong to; otherwise a later
        # save with a new host would send them there without the host-change check firing.
        cleared = any([await store.get(K.GRAFANA_PASSWORD) is not None,
                       await store.get(K.GRAFANA_TOKEN) is not None])
        await store.delete(K.GRAFANA_PASSWORD)
        await store.delete(K.GRAFANA_TOKEN)
        new_password = new_token = ""
    if new_password:
        await store.set(K.GRAFANA_PASSWORD, new_password, secret=True)
    if new_token:
        await store.set(K.GRAFANA_TOKEN, new_token, secret=True)
    await _set_or_delete(store, K.GRAFANA_DS_UID, ds_uid)
    await _set_or_delete(store, K.GRAFANA_QUERIES, queries)
    await record(db, user, "grafana", "settings:grafana", {
        "url": sanitize(url), "user": username, "ds_uid": ds_uid,
        "password_changed": bool(new_password), "token_changed": bool(new_token),
        "queries_overridden": sorted(queries), "credentials_cleared": cleared,
    })
    if form.get("action") != "test":
        return None, None, False

    password = await store.get(K.GRAFANA_PASSWORD)
    token = await store.get(K.GRAFANA_TOKEN)
    if not url:
        return "Enter the Grafana URL first.", None, True
    client = request.app.state.grafana_factory(url, username or None, password, token)
    try:
        found = await client.list_datasources()
    except Exception as e:  # noqa: BLE001 - shown to the admin
        await record(db, user, "grafana_test", "settings:grafana", {"ok": False})
        return f"Connection failed — {describe_error(e, (password, token))}", None, True
    proms = [{"uid": d.uid, "name": d.name} for d in found if d.type == "prometheus"]
    await store.set(DS_CACHE_KEY, proms)
    await record(db, user, "grafana_test", "settings:grafana", {"ok": True, "found": len(proms)})
    return None, f"Connection OK — {len(proms)} Prometheus datasource(s) found.", True


def _parse_sa_json(raw: str) -> str | None:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") != "service_account":
        return None
    if not data.get("client_email") or not data.get("private_key"):
        return None
    return raw


async def handle_calendar_post(
    request: Request, db: AsyncSession, store: SettingsStore, user: User | None
) -> tuple[str | None, str | None, bool]:
    """Save the Calendar form; with action=test also run probe(). Returns (error, message, tested)."""
    form = await request.form()
    calendar_id = str(form.get("calendar_id", "")).strip()
    upload = form.get("sa_json")
    raw = ""
    if upload is not None:
        content = upload if isinstance(upload, str) else await upload.read()
        raw = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
    new_key = None
    if raw.strip():
        new_key = _parse_sa_json(raw)
        if new_key is None:
            return "That file is not a Google service-account JSON key.", None, False

    if new_key:
        await store.set(K.GCAL_SA_JSON, new_key, secret=True)
    await _set_or_delete(store, K.GCAL_CALENDAR_ID, calendar_id)
    await record(db, user, "calendar", "settings:calendar",
                 {"calendar_id": calendar_id, "key_changed": bool(new_key)})
    if form.get("action") != "test":
        return None, None, False

    sa_json = await store.get(K.GCAL_SA_JSON)
    if not sa_json or not calendar_id:
        return "Upload the service-account key and enter the calendar id first.", None, True
    secrets = [sa_json]
    try:
        secrets.append(json.loads(sa_json).get("private_key"))
        client = await asyncio.to_thread(
            request.app.state.calendar_factory, sa_json, calendar_id
        )
        await client.probe()
    except Exception as e:  # noqa: BLE001 - shown to the admin
        await record(db, user, "calendar_test", "settings:calendar", {"ok": False})
        return f"Calendar test failed — {describe_error(e, secrets)}", None, True
    await record(db, user, "calendar_test", "settings:calendar", {"ok": True})
    return None, "Calendar test OK — a test event was created and deleted.", True


# --- index / integrations -------------------------------------------------------------------


@router.get("")
async def admin_index(user: User = Depends(admin_gate)):
    return redirect("/admin/integrations")


async def _render_integrations(
    request: Request, db: AsyncSession, store: SettingsStore, user: User,
    status: int = 200, errors: dict | None = None, messages: dict | None = None,
) -> HTMLResponse:
    topics = (await db.scalars(
        select(TelegramTopic).order_by(TelegramTopic.last_seen_at.desc())
    )).all()
    posts = await store.get(K.TG_TOPIC_POSTS) or {}
    return render(request, "admin/integrations.html", {
        "user": user, "section": "integrations",
        "telegram": {
            "token_set": await store.get(K.TG_TOKEN) is not None,
            "bot_username": await store.get(K.TG_BOT_USERNAME) or "",
            "group_id": await store.get(K.TG_GROUP_ID),
            "group_title": await store.get(K.TG_GROUP_TITLE) or "",
            "topic_id": await store.get(K.TG_TOPIC_ID),
            "topics": topics,
            "posts": [(k, label, posts.get(k, True)) for k, label in TOPIC_POSTS],
        },
        "grafana": await grafana_ctx(store),
        "calendar": await calendar_ctx(store),
        "portal_base_url": await store.get(K.PORTAL_BASE_URL) or "",
        "errors": errors or {}, "messages": messages or {},
        "placeholder": SECRET_PLACEHOLDER,
    }, status)


@router.get("/integrations")
async def integrations_page(
    request: Request, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    return await _render_integrations(request, db, store, user)


@router.post("/integrations/telegram")
async def integrations_telegram(
    request: Request, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    form = await request.form()
    token = str(form.get("token", "")).strip()
    raw_topic = str(form.get("topic_id", "")).strip()
    topic_id = None
    if raw_topic:
        topic_id = _opt_int(raw_topic)
        if topic_id is None or await db.get(TelegramTopic, topic_id) is None:
            return await _render_integrations(
                request, db, store, user, 400, {"telegram": "Choose a topic from the list."})
    username = None
    if token:
        try:
            me = await request.app.state.telegram_factory(token).get_me()
        except Exception as e:  # noqa: BLE001 - shown to the admin
            return await _render_integrations(request, db, store, user, 400, {
                "telegram": f"Telegram rejected the token — {describe_error(e, [token])}"})
        username = me.username
        await store.set(K.TG_TOKEN, token, secret=True)
        await store.set(K.TG_BOT_USERNAME, username)
    if topic_id is not None:
        await store.set(K.TG_TOPIC_ID, topic_id)
    posts = {k: form.get(f"post_{k}") == "on" for k, _ in TOPIC_POSTS}
    await store.set(K.TG_TOPIC_POSTS, posts)
    await record(db, user, "telegram", "settings:telegram", {
        "token_changed": bool(token), "bot_username": username, "topic_id": topic_id,
        "topic_posts": posts,
    })
    await db.commit()
    return redirect("/admin/integrations")


@router.post("/integrations/grafana")
async def integrations_grafana(
    request: Request, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    error, message, tested = await handle_grafana_post(request, db, store, user)
    await db.commit()  # an invalid form wrote nothing
    if error or tested:
        return await _render_integrations(
            request, db, store, user, 400 if error else 200,
            {"grafana": error} if error else None, {"grafana": message} if message else None)
    return redirect("/admin/integrations")


@router.post("/integrations/calendar")
async def integrations_calendar(
    request: Request, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    error, message, tested = await handle_calendar_post(request, db, store, user)
    await db.commit()  # an invalid form wrote nothing
    if error or tested:
        return await _render_integrations(
            request, db, store, user, 400 if error else 200,
            {"calendar": error} if error else None, {"calendar": message} if message else None)
    return redirect("/admin/integrations")


@router.post("/integrations/portal")
async def integrations_portal(
    request: Request, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    form = await request.form()
    url = str(form.get("base_url", "")).strip().rstrip("/")
    if not valid_http_url(url):
        return await _render_integrations(request, db, store, user, 400, {
            "portal": "The portal URL must start with http:// or https://."})
    await store.set(K.PORTAL_BASE_URL, url)
    await record(db, user, "portal", "settings:portal", {"base_url": sanitize(url)})
    await db.commit()
    return redirect("/admin/integrations")


# --- servers --------------------------------------------------------------------------------


async def _grafana_ready(store: SettingsStore) -> bool:
    return bool(await store.get(K.GRAFANA_URL) and await store.get(K.GRAFANA_DS_UID))


async def _render_servers(
    request: Request, db: AsyncSession, store: SettingsStore, user: User, status: int = 200,
    notice: str | None = None, fetched: dict | None = None, show_deleted: bool = False,
    error: str | None = None,
) -> HTMLResponse:
    archived = await inventory.list_archived(db)
    return render(request, "admin/servers.html", {
        "user": user, "section": "servers", "servers": await inventory.list_servers(db),
        "notice": notice, "fetched": fetched, "grafana_ready": await _grafana_ready(store),
        "archived": archived if show_deleted else [], "archived_count": len(archived),
        "show_deleted": show_deleted, "error": error,
        "tz": (await store.get_rules()).timezone,
    }, status)


def _server_values(server: Server | None) -> dict:
    if server is None:
        return {"name": "", "hostname": "", "kind": "grafana", "grafana_instance": "",
                "has_gpu": False, "gpu_count": 0, "enabled": True,
                "notes": ""}
    return {"name": server.name, "hostname": server.hostname, "kind": server.kind,
            "grafana_instance": server.grafana_instance or "", "has_gpu": server.has_gpu,
            "gpu_count": server.gpu_count, "enabled": server.enabled, "notes": server.notes or ""}


def _render_server_edit(
    request: Request, user: User, server_id: int | None, values: dict,
    errors: list[str] | None = None, status: int = 200,
) -> HTMLResponse:
    return render(request, "admin/server_edit.html", {
        "user": user, "section": "servers", "server_id": server_id, "values": values,
        "errors": errors or [], "kinds": KINDS,
    }, status)


async def _parse_server_form(
    db: AsyncSession, form, server_id: int | None
) -> tuple[dict, dict, list[str]]:
    """Returns (values for re-rendering, model fields, errors)."""
    values = {
        "name": str(form.get("name", "")).strip(),
        "hostname": str(form.get("hostname", "")).strip(),
        "kind": str(form.get("kind", "")),
        "grafana_instance": str(form.get("grafana_instance", "")).strip(),
        "has_gpu": form.get("has_gpu") == "on",
        "gpu_count": str(form.get("gpu_count", "0")).strip() or "0",
        "enabled": form.get("enabled") == "on",
        "notes": str(form.get("notes", "")).strip(),
    }
    errors = []
    if not values["name"]:
        errors.append("Enter a name.")
    else:
        q = select(Server.id).where(func.lower(Server.name) == values["name"].lower(),
                                    Server.archived_at.is_(None))
        if server_id is not None:
            q = q.where(Server.id != server_id)
        if await db.scalar(q) is not None:
            errors.append(f"A server named “{values['name']}” already exists.")
    if not values["hostname"]:
        errors.append("Enter a hostname.")
    if values["kind"] not in KINDS:
        errors.append("Choose Grafana-monitored or standalone.")
    try:
        gpu_count = int(values["gpu_count"])
    except ValueError:
        gpu_count = -1
    if not values["has_gpu"]:
        gpu_count = 0
    elif not 1 <= gpu_count <= 64:
        errors.append("GPU count must be a whole number from 1 to 64 for a server with a GPU.")
    fields = {
        "name": values["name"], "hostname": values["hostname"], "kind": values["kind"],
        "grafana_instance": values["grafana_instance"] or None, "has_gpu": values["has_gpu"],
        "gpu_count": gpu_count, "enabled": values["enabled"],
        "notes": values["notes"] or None,
    }
    return values, fields, errors


@router.get("/servers")
async def servers_page(
    request: Request, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    notice = None
    params = request.query_params
    raw = params.get("added", "")
    if raw.isascii() and raw.isdigit() and 0 <= int(raw) <= MAX_IMPORT_ROWS:
        added = int(raw)
        notice = f"Added {added} server{'' if added == 1 else 's'}."
    if (sid := _opt_int(params.get("deleted_id"))) is not None:
        server = await db.get(Server, sid)
        if server is not None and server.archived_at is not None:
            notice = f"Deleted {server.name}. Its booking history is kept."
    if (sid := _opt_int(params.get("restored_id"))) is not None:
        server = await db.get(Server, sid)
        if server is not None and server.archived_at is None:
            notice = f"Restored {server.name}. It is disabled — edit it to enable bookings."
    if params.get("purged") == "1":
        notice = "Server permanently deleted."
    return await _render_servers(request, db, store, user, notice=notice,
                                 show_deleted=params.get("deleted") == "1")


# --- same-machine check (manual form and import) --------------------------------------------


def _duplicate_error(name: str) -> str:
    return f'This machine already exists as "{name}".'


async def _machine_index(
    request: Request, db: AsyncSession, extra: list[set[str]], resolve: bool = True
) -> MachineIndex:
    """Index of every existing server; DNS for them and `extra` is resolved once, bounded."""
    index = MachineIndex(request.app.state.resolver)
    await index.load([(s.id, s.name, machine_keys(s.hostname, s.grafana_instance))
                      for s in await inventory.list_servers(db)], extra, resolve=resolve)
    return index


async def _check_same_machine(
    request: Request, db: AsyncSession, fields: dict, errors: list[str],
    server: Server | None,
) -> None:
    keys = machine_keys(fields["hostname"], fields["grafana_instance"])
    if not keys:
        return
    # Editing without touching hostname/instance: compare names only, so a later DNS change
    # that makes two servers share an address never blocks toggling enabled or notes.
    use_addresses = server is None or keys != machine_keys(server.hostname,
                                                           server.grafana_instance)
    index = await _machine_index(request, db, [keys], resolve=use_addresses)
    other = index.find(keys, exclude_id=server.id if server else None,
                       use_addresses=use_addresses)
    if other is not None:
        errors.append(_duplicate_error(other))


# --- import from Grafana --------------------------------------------------------------------


def _configured_gpus(server: Server) -> int:
    return server.gpu_count if server.has_gpu else 0


async def _mark_existing(request: Request, db: AsyncSession, rows: list[dict]) -> MachineIndex:
    """Flag rows that are already a server (same machine, or — when not ticked — same name).

    Rows that are the same machine as a server also get its id and configured GPU count, and
    `gpu_mismatch` when Grafana's count (if it could be read: `gpu_known`) differs.
    """
    index = await _machine_index(request, db, [machine_keys(r["instance"]) for r in rows])
    servers = {s.id: s for s in await inventory.list_servers(db)}
    names = {s.name.lower() for s in servers.values()}
    for r in rows:
        found = index.match(machine_keys(r["instance"]))
        server = servers.get(found[0]) if found else None
        r["existing"] = found[1] if found else None
        r["existing_id"] = server.id if server else None
        r["configured_gpus"] = _configured_gpus(server) if server else None
        r["gpu_mismatch"] = bool(server and r.get("gpu_known")
                                 and str(r["gpu_count"]) != str(r["configured_gpus"]))
        r["exists"] = (r["existing"] is not None
                       or (not r.get("selected") and r["name"].lower() in names))
    return index


def _render_import(
    request: Request, user: User, rows: list[dict] | None = None, *, configured: bool = True,
    error: str | None = None, gpu_note: str | None = None, form_error: str | None = None,
    notes: list[str] | None = None, notice: str | None = None, status: int = 200,
) -> HTMLResponse:
    return render(request, "admin/servers_import.html", {
        "user": user, "section": "servers", "configured": configured, "rows": rows,
        "error": error, "gpu_note": gpu_note, "form_error": form_error, "notes": notes or [],
        "notice": notice,
        "gpu_mismatches": sum(1 for r in rows or [] if r.get("gpu_mismatch")),
    }, status)


async def _discover(request: Request, store: SettingsStore):
    """Run Grafana discovery (needs _grafana_ready).

    Returns (hosts, error, gpu_error): hosts is None when discovery failed (error says why);
    gpu_error describes why GPU counts could not be read (counts are then all 0).
    """
    password = await store.get(K.GRAFANA_PASSWORD)
    token = await store.get(K.GRAFANA_TOKEN)
    client = request.app.state.grafana_factory(
        await store.get(K.GRAFANA_URL), await store.get(K.GRAFANA_USER), password, token)
    queries = effective_queries(await store.get(K.GRAFANA_QUERIES))
    try:
        hosts = await discover_hosts(client, await store.get(K.GRAFANA_DS_UID), queries)
    except Exception as e:  # noqa: BLE001 - shown to the admin
        return None, f"Grafana query failed — {describe_error(e, (password, token))}", None
    gpu_error = None
    if hosts.gpu_error is not None:
        gpu_error = describe_error(hosts.gpu_error, (password, token))
    return hosts, None, gpu_error


@router.get("/servers/import")
async def servers_import_page(
    request: Request, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    if not await _grafana_ready(store):
        return _render_import(request, user, configured=False)
    hosts, error, gpu_error = await _discover(request, store)
    if error is not None:
        return _render_import(request, user, error=error)
    notes = []
    if len(hosts) > MAX_IMPORT_ROWS:
        notes.append(f"Grafana returned {len(hosts)} hosts. Showing the first "
                     f"{MAX_IMPORT_ROWS} hosts; import them, then reload for the rest.")
    gpu_known = gpu_error is None
    rows = [{"instance": h.instance, "name": h.name, "gpu_count": h.gpu_count,
             "selected": False, "errors": [], "gpu_known": gpu_known}
            for h in hosts[:MAX_IMPORT_ROWS]]
    await _mark_existing(request, db, rows)
    gpu_note = None
    if not gpu_known:
        gpu_note = f"GPU count could not be read — set it by hand. ({gpu_error})"
    notice = None
    raw = request.query_params.get("gpu_synced", "")
    if raw.isascii() and raw.isdigit() and 0 <= int(raw) <= MAX_IMPORT_ROWS:
        n = int(raw)
        notice = f"Updated GPU counts for {n} server{'' if n == 1 else 's'}."
    return _render_import(request, user, rows, gpu_note=gpu_note, notes=notes, notice=notice)


@router.post("/servers/import/sync-gpus")
async def servers_import_sync_gpus(
    request: Request, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    """Set gpu_count/has_gpu of every live server Grafana reports a different GPU count for."""
    if not await _grafana_ready(store):
        return _render_import(request, user, configured=False)
    hosts, error, gpu_error = await _discover(request, store)
    if error is None and gpu_error is not None:
        error = f"GPU counts could not be read from Grafana — nothing was changed. ({gpu_error})"
    if error is not None:
        return _render_import(request, user, error=error)
    rows = [{"instance": h.instance, "name": h.name, "gpu_count": h.gpu_count,
             "selected": False, "gpu_known": True} for h in hosts]
    await _mark_existing(request, db, rows)
    changes: list[dict] = []
    done: set[int] = set()
    for r in rows:
        sid, new = r["existing_id"], r["gpu_count"]
        if not r["gpu_mismatch"] or sid in done or not 0 <= new <= 64:
            continue  # 64 is the form's limit; a larger count is left for the admin
        done.add(sid)
        server = await inventory.update_server(db, sid, gpu_count=new, has_gpu=new > 0)
        changes.append({"server_id": sid, "name": server.name, "instance": r["instance"],
                        "from": r["configured_gpus"], "to": new})
    await record(db, user, "servers_gpu_sync", "servers", {"changes": changes})
    await db.commit()
    return redirect(f"/admin/servers/import?gpu_synced={len(changes)}")


def _is_zero(raw: str) -> bool:
    try:
        return int(raw) == 0
    except ValueError:
        return False


def _import_rows(form) -> list[dict]:
    rows = []
    for i in range(MAX_IMPORT_ROWS):
        if f"instance_{i}" not in form:
            break
        rows.append({
            "instance": str(form.get(f"instance_{i}", "")).strip(),
            "name": str(form.get(f"name_{i}", "")).strip(),
            "gpu_count": str(form.get(f"gpu_count_{i}", "0")).strip() or "0",
            "selected": form.get(f"selected_{i}") == "on",
            "errors": [],
        })
    return rows


@router.post("/servers/import")
async def servers_import(
    request: Request, user: User = Depends(admin_gate), db: AsyncSession = Depends(get_db)
):
    rows = _import_rows(await request.form())
    index = await _mark_existing(request, db, rows)
    selected = [r for r in rows if r["selected"]]
    if not selected:
        for r in rows:
            r["selected"] = False
        return _render_import(request, user, rows, form_error="Tick at least one server to add.",
                              status=400)
    to_create = []
    batch_names: set[str] = set()
    for r in selected:
        # Same validation as the manual form; has_gpu follows from the GPU count.
        _, fields, errors = await _parse_server_form(db, {
            "name": r["name"], "hostname": r["instance"], "kind": "grafana",
            "grafana_instance": r["instance"], "has_gpu": "" if _is_zero(r["gpu_count"]) else "on",
            "gpu_count": r["gpu_count"], "enabled": "on", "notes": "",
        }, None)
        # Existing servers and rows accepted earlier in this batch (port/case/IP-aware).
        keys = machine_keys(r["instance"])
        if keys and (other := index.find(keys)) is not None:
            errors.append(_duplicate_error(other))
        if r["name"] and r["name"].lower() in batch_names:
            errors.append(f"A server named “{r['name']}” already exists in this import.")
        batch_names.add(r["name"].lower())
        index.add(r["name"] or r["instance"], keys)
        r["errors"] = errors
        to_create.append(fields)
    if any(r["errors"] for r in selected):
        return _render_import(request, user, rows, form_error="Nothing was added — fix the "
                              "rows marked below.", status=400)
    try:
        async with db.begin_nested():
            for fields in to_create:
                await inventory.create_server(db, **fields)
    except IntegrityError:
        return _render_import(request, user, rows, form_error="Nothing was added — a server "
                              "with one of these names already exists.", status=400)
    created = [f["name"] for f in to_create]
    await record(db, user, "servers_import", "servers", {
        "names": created, "instances": [f["grafana_instance"] for f in to_create]})
    await db.commit()
    return redirect(f"/admin/servers?added={len(created)}")


@router.get("/servers/new")
async def server_new_page(request: Request, user: User = Depends(admin_gate)):
    return _render_server_edit(request, user, None, _server_values(None))


@router.post("/servers/new")
async def server_create(
    request: Request, user: User = Depends(admin_gate), db: AsyncSession = Depends(get_db)
):
    values, fields, errors = await _parse_server_form(db, await request.form(), None)
    await _check_same_machine(request, db, fields, errors, None)
    if errors:
        return _render_server_edit(request, user, None, values, errors, 400)
    try:
        async with db.begin_nested():
            server = await inventory.create_server(db, **fields)
    except IntegrityError:
        return _render_server_edit(request, user, None, values,
                                   [f"A server named “{values['name']}” already exists."], 400)
    await record(db, user, "server_create", f"server:{server.id}", fields)
    await db.commit()
    return redirect("/admin/servers")


@router.get("/servers/{server_id}")
async def server_edit_page(
    request: Request, server_id: int, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db),
):
    server = await inventory.get_live(db, check_id(server_id))
    return _render_server_edit(request, user, server.id, _server_values(server))


@router.post("/servers/{server_id}")
async def server_update(
    request: Request, server_id: int, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    server = await inventory.get_live(db, check_id(server_id))
    values, fields, errors = await _parse_server_form(db, await request.form(), server.id)
    await _check_same_machine(request, db, fields, errors, server)
    if errors:
        return _render_server_edit(request, user, server.id, values, errors, 400)
    was_enabled = server.enabled
    try:
        async with db.begin_nested():
            await inventory.update_server(db, server.id, **fields)
    except IntegrityError:
        await db.refresh(server)
        return _render_server_edit(request, user, server.id, values,
                                   [f"A server named “{values['name']}” already exists."], 400)
    await record(db, user, "server_update", f"server:{server.id}", fields)
    await db.commit()
    if was_enabled and not fields["enabled"]:
        remaining = await db.scalar(select(func.count()).select_from(Booking).where(
            Booking.server_id == server.id, Booking.status == "active",
            Booking.end_at > clock.utcnow(),
        ))
        if remaining:
            noun = "booking remains" if remaining == 1 else "bookings remain"
            return await _render_servers(
                request, db, store, user,
                notice=f"{server.name} disabled. {remaining} {noun} on this server.")
    return redirect("/admin/servers")


@router.post("/servers/{server_id}/fetch")
async def server_fetch(
    request: Request, server_id: int, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    server = await inventory.get_live(db, check_id(server_id))
    url = await store.get(K.GRAFANA_URL)
    ds_uid = await store.get(K.GRAFANA_DS_UID)
    password = await store.get(K.GRAFANA_PASSWORD)
    token = await store.get(K.GRAFANA_TOKEN)
    ok, text = False, ""
    if server.kind != "grafana":
        text = "This server is standalone — it is not monitored by Grafana."
    elif not url or not ds_uid:
        text = "Grafana is not configured (URL and datasource are required)."
    else:
        client = request.app.state.grafana_factory(
            url, await store.get(K.GRAFANA_USER), password, token)
        queries = effective_queries(await store.get(K.GRAFANA_QUERIES))
        try:
            u = await fetch_usage(client, ds_uid, queries,
                                  server.grafana_instance or server.hostname,
                                  server.gpu_count, clock.utcnow())
        except Exception as e:  # noqa: BLE001 - shown to the admin
            text = describe_error(e, (password, token))
        else:
            ok = True
            await usage_svc.save_snapshot(db, server.id, u)  # board shows it right away
            parts = [f"CPU {u.cpu_pct:.1f}%", f"RAM {u.ram_pct:.1f}%"]
            parts += [f"{g.label(i, len(u.gpus))}: util {g.util_pct:.1f}% mem {g.mem_pct:.1f}%"
                      for i, g in enumerate(u.gpus)]
            parts.append("disks " + (", ".join(f"{d.mount} {d.used_pct:.1f}%" for d in u.disks)
                                     if u.disks else "none reported"))
            text = ", ".join(parts)
    await record(db, user, "fetch_usage", f"server:{server.id}", {"ok": ok})
    await db.commit()
    return await _render_servers(request, db, store, user,
                                 fetched={"server_id": server.id, "ok": ok, "text": text})


# --- delete (archive), restore, delete permanently ------------------------------------------


async def _archived(db: AsyncSession, server_id: int) -> Server:
    server = await db.get(Server, check_id(server_id))
    if server is None or server.archived_at is None:
        raise LookupError(f"deleted server {server_id} not found")
    return server


@router.get("/servers/{server_id}/delete")
async def server_delete_page(
    request: Request, server_id: int, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db),
):
    server = await inventory.get_live(db, check_id(server_id))
    return render(request, "admin/server_delete.html", {
        "user": user, "section": "servers", "server": server, "permanent": False,
        "upcoming": await server_archive.count_upcoming(db, server.id, clock.utcnow()),
    })


@router.post("/servers/{server_id}/delete")
async def server_delete(
    request: Request, server_id: int, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    server = await inventory.get_live(db, check_id(server_id))
    name = server.name
    cancelled = await server_archive.archive(db, store, server.id, user, clock.utcnow())
    await record(db, user, "server_delete", f"server:{server.id}",
                 {"name": name, "cancelled_bookings": cancelled})
    await db.commit()
    return redirect(f"/admin/servers?deleted_id={server.id}")


async def _restore_conflict(request: Request, db: AsyncSession, server: Server) -> str | None:
    """Why the server cannot come back: a live server has its name or is the same machine."""
    taken = await db.scalar(select(Server.name).where(
        func.lower(Server.name) == server.name.lower(), Server.archived_at.is_(None)))
    if taken is not None:
        return (f"Cannot restore {server.name}: a server named “{taken}” already exists. "
                "Rename or delete that server first.")
    errors: list[str] = []
    fields = {"hostname": server.hostname, "grafana_instance": server.grafana_instance}
    await _check_same_machine(request, db, fields, errors, None)  # live servers only
    if errors:
        return f"Cannot restore {server.name}: {errors[0]}"
    return None


@router.post("/servers/{server_id}/restore")
async def server_restore(
    request: Request, server_id: int, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    server = await _archived(db, server_id)
    error = await _restore_conflict(request, db, server)
    if error is None:
        try:
            async with db.begin_nested():
                await server_archive.restore(db, server.id)
        except IntegrityError:  # a server with the same name was added meanwhile
            await db.refresh(server)
            error = f"Cannot restore {server.name}: a server with that name already exists."
    if error is not None:
        return await _render_servers(request, db, store, user, 409, show_deleted=True,
                                     error=error)
    await record(db, user, "server_restore", f"server:{server.id}", {"name": server.name})
    await db.commit()
    return redirect(f"/admin/servers?restored_id={server.id}")


@router.get("/servers/{server_id}/purge")
async def server_purge_page(
    request: Request, server_id: int, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db),
):
    server = await _archived(db, server_id)
    return render(request, "admin/server_delete.html", {
        "user": user, "section": "servers", "server": server, "permanent": True,
        "counts": await server_archive.purge_counts(db, server.id),
    })


@router.post("/servers/{server_id}/purge")
async def server_purge(
    request: Request, server_id: int, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    server = await _archived(db, server_id)
    sid = server.id
    try:
        name, counts = await server_archive.purge(db, sid)
    except server_archive.CalendarCleanupPending:  # raised before anything was written
        return await _render_servers(
            request, db, store, user, 409, show_deleted=True,
            error=f"{server.name} cannot be erased yet: its Calendar events are still being "
                  "removed. Try again in a minute (see Health if this persists).")
    await record(db, user, "server_purge", f"server:{sid}", {
        "name": name, "bookings": counts.bookings, "snapshots": counts.snapshots,
        "subscriptions": counts.subscriptions})
    await db.commit()
    return redirect("/admin/servers?deleted=1&purged=1")


# --- rules ----------------------------------------------------------------------------------

RULE_LABELS = {
    "timezone": "Time zone",
    "max_booking_hours": "Maximum booking length (hours)",
    "soon_window_min": "“Soon” window (minutes)",
    "work_days": "Working days",
    "work_start": "Working hours start",
    "work_end": "Working hours end",
    "idle_window_min": "Idle window (minutes)",
    "idle_gpu_util_pct": "Idle: GPU utilisation below (%)",
    "idle_cpu_pct": "Idle: CPU below (%)",
    "idle_ram_delta_pp": "Idle: RAM change below (percentage points)",
    "max_idle_warnings": "Idle warnings before reclaim",
    "ram_alert_pct": "Warn when RAM is at least (%)",
    "disk_alert_pct": "Warn when a disk is at least (%)",
    "stale_after_min": "Metrics stale after (minutes)",
    "snapshot_retention_h": "Keep snapshots (hours)",
}
RULE_MAX = {  # upper bounds keep timedelta arithmetic in the jobs from overflowing
    "max_booking_hours": 8760,
    "soon_window_min": 10080,
    "idle_window_min": 10080,
    "max_idle_warnings": 20,
    "stale_after_min": 10080,
    "snapshot_retention_h": 720,
}
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _rules_values(r: Rules) -> dict:
    return {**r.to_dict(), "work_days": list(r.work_days)}


def parse_rules_form(form) -> tuple[dict, Rules | None, list[str]]:
    values: dict = {}
    errors: list[str] = []
    kw: dict = {}
    for f in dc_fields(Rules):
        label = RULE_LABELS[f.name]
        if f.name == "work_days":
            raw_days = [str(d) for d in form.getlist("work_days")]
            values["work_days"] = [int(d) for d in raw_days if d.isdigit()]
            if any(d not in {str(i) for i in range(7)} for d in raw_days):
                errors.append(f"{label}: choose days from Monday to Sunday.")
            else:
                kw["work_days"] = tuple(sorted({int(d) for d in raw_days}))
            continue
        raw = str(form.get(f.name, "")).strip()
        values[f.name] = raw
        if f.name == "timezone":
            try:
                ZoneInfo(raw)
                kw[f.name] = raw
            except (ZoneInfoNotFoundError, ValueError):
                errors.append(f"{label}: “{raw}” is not a valid time zone.")
        elif f.name in ("work_start", "work_end"):
            if HHMM.fullmatch(raw):
                kw[f.name] = raw
            else:
                errors.append(f"{label}: use HH:MM (24-hour).")
        elif f.type in (int, "int"):
            try:
                n = int(raw)
            except ValueError:
                n = 0
            if n < 1:
                errors.append(f"{label}: must be a positive whole number.")
            elif n > RULE_MAX[f.name]:
                errors.append(f"{label}: must be at most {RULE_MAX[f.name]}.")
            else:
                kw[f.name] = n
        else:
            try:
                x = float(raw)
            except ValueError:
                x = -1.0
            if not 0 <= x <= 100:
                errors.append(f"{label}: must be between 0 and 100.")
            else:
                kw[f.name] = x
    if "work_start" in kw and "work_end" in kw and kw["work_start"] >= kw["work_end"]:
        errors.append("Working hours start must be before the end.")
    return values, None if errors else Rules(**kw), errors


def _render_rules(request: Request, user: User, values: dict, errors=None, status: int = 200):
    return render(request, "admin/rules.html", {
        "user": user, "section": "rules", "values": values, "errors": errors or [],
        "labels": RULE_LABELS, "day_names": DAY_NAMES,
        "fields": [f.name for f in dc_fields(Rules)],
    }, status)


@router.get("/rules")
async def rules_page(
    request: Request, user: User = Depends(admin_gate), store: SettingsStore = Depends(get_store)
):
    return _render_rules(request, user, _rules_values(await store.get_rules()))


@router.post("/rules")
async def rules_save(
    request: Request, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    values, rules, errors = parse_rules_form(await request.form())
    if rules is None:
        return _render_rules(request, user, values, errors, 400)
    await store.set_rules(rules)
    await record(db, user, "rules", "settings:rules", rules.to_dict())
    await db.commit()
    return redirect("/admin/rules")


# --- bookings -------------------------------------------------------------------------------


@router.get("/bookings")
async def bookings_page(
    request: Request, server_id: str | None = None, user_id: str | None = None,
    status: str | None = None, deleted: str | None = None, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    sid, uid = _opt_int(server_id), _opt_int(user_id)
    st = status if status in STATUSES else None
    show_deleted = deleted == "1"
    q = (select(Booking, User.display_name, Server.name, Server.archived_at.is_not(None))
         .join(User, User.id == Booking.user_id).join(Server, Server.id == Booking.server_id))
    if not show_deleted:
        q = q.where(Server.archived_at.is_(None))
    if sid is not None:
        q = q.where(Booking.server_id == sid)
    if uid is not None:
        q = q.where(Booking.user_id == uid)
    if st is not None:
        q = q.where(Booking.status == st)
    rows = (await db.execute(
        q.order_by(Booking.start_at.desc(), Booking.id.desc()).limit(LIST_LIMIT)
    )).all()
    rules = await store.get_rules()
    return render(request, "admin/bookings.html", {
        "user": user, "section": "bookings", "rows": rows, "now": clock.utcnow(),
        "tz": rules.timezone,
        "servers": await inventory.list_servers(db)
        + (await inventory.list_archived(db) if show_deleted else []),
        "users": (await db.scalars(select(User).order_by(User.display_name))).all(),
        "statuses": STATUSES,
        "filters": {"server_id": sid, "user_id": uid, "status": st, "deleted": show_deleted},
    })


@router.post("/bookings/{booking_id}/cancel")
async def bookings_cancel(
    request: Request, booking_id: int, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    form = await request.form()
    reason = str(form.get("reason", "")).strip()[:500] or None
    await booking.cancel_booking(db, store, check_id(booking_id), actor=user,
                                 now=clock.utcnow(), reason=reason)
    await record(db, user, "booking_cancel", f"booking:{booking_id}", {"reason": reason})
    await db.commit()
    return redirect("/admin/bookings")


# --- health ---------------------------------------------------------------------------------


WORKER_SILENT_AFTER = timedelta(minutes=2)  # the worker reconciles every 10 s


def _iso(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@router.get("/health")
async def health_page(
    request: Request, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    now = clock.utcnow()
    cards = await board.build_board(db, store, now)
    outbox: dict[str, dict[str, int]] = {}
    for label, cond in (
        ("pending", OutboxItem.failed_at.is_(None)),
        ("failed", OutboxItem.failed_at.is_not(None)),
    ):
        rows = await db.execute(
            select(OutboxItem.kind, func.count()).where(OutboxItem.done_at.is_(None), cond)
            .group_by(OutboxItem.kind)
        )
        for kind, n in rows.all():
            outbox.setdefault(kind, {"pending": 0, "failed": 0})[label] = n
    missing = [label for key, label in (
        (K.TG_TOKEN, "bot token"), (K.TG_GROUP_ID, "group"), (K.TG_TOPIC_ID, "topic"),
    ) if await store.get(key) is None]
    last_poll = await store.get(HEALTH_KEY)
    poll_at = _iso(last_poll.get("at")) if last_poll else None
    last_ok_at = _iso(last_poll.get("last_ok_at")) if last_poll else None
    heartbeat = await store.get(K.WORKER_HEARTBEAT)
    worker_at = _iso(heartbeat.get("at")) if isinstance(heartbeat, dict) else None
    if last_poll and isinstance(last_poll.get("failed"), dict):
        secrets = (await store.get(K.GRAFANA_PASSWORD), await store.get(K.GRAFANA_TOKEN))
        last_poll = {**last_poll, "failed": {
            name: sanitize(str(err), secrets) for name, err in last_poll["failed"].items()}}
    rules = await store.get_rules()
    return render(request, "admin/health.html", {
        "user": user, "section": "health", "last_poll": last_poll, "poll_at": poll_at,
        "last_ok_at": last_ok_at, "heartbeat": heartbeat if worker_at else None,
        "worker_at": worker_at,
        "worker_silent": worker_at is None or now - worker_at > WORKER_SILENT_AFTER,
        "tz": rules.timezone,
        "stale": [c.server.name for c in cards if c.stale and c.server.enabled],
        "outbox": sorted(outbox.items()), "bot_missing": missing,
        "blocked": (await db.scalars(
            select(User).where(User.dm_blocked.is_(True)).order_by(User.display_name)
        )).all(),
    })


@router.post("/health/retry")
async def health_retry(
    request: Request, user: User = Depends(admin_gate), db: AsyncSession = Depends(get_db)
):
    form = await request.form()
    kind = str(form.get("kind", "")).strip()
    if not kind:
        raise LookupError("no outbox kind given")
    result = await db.execute(
        update(OutboxItem)
        .where(OutboxItem.kind == kind, OutboxItem.done_at.is_(None))
        .values(failed_at=None, next_attempt_at=clock.utcnow())
    )
    await record(db, user, "outbox_retry", f"outbox:{kind}", {"count": result.rowcount})
    await db.commit()
    return redirect("/admin/health")


# --- audit ----------------------------------------------------------------------------------


@router.get("/audit")
async def audit_page(
    request: Request, user: User = Depends(admin_gate),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    rows = (await db.execute(
        select(AuditLog, User.display_name)
        .outerjoin(User, User.id == AuditLog.actor_user_id)
        .order_by(AuditLog.id.desc()).limit(LIST_LIMIT)
    )).all()
    rules = await store.get_rules()
    return render(request, "admin/audit.html", {
        "user": user, "section": "audit", "rows": rows, "tz": rules.timezone,
    })
