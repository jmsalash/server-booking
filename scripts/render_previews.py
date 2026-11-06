"""Dev-only: render every portal/admin page with sample data, for visual review.

Not used in production. Starts a throwaway Postgres (testcontainers, like tests/conftest.py),
seeds realistic data, logs in as an admin by creating a session and setting the signed cookie,
fetches each page in-process and writes self-contained HTML (app.css inlined, scripts removed)
to OUT_DIR. Then screenshot them, e.g.:

    google-chrome --headless=new --disable-gpu --hide-scrollbars --window-size=1280,900 \
        --screenshot=board.png file://$OUT_DIR/board.html

Usage: .venv/bin/python scripts/render_previews.py [OUT_DIR] [--shots]
"""

import asyncio
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine
# Ryuk (the reaper container) is optional; the `with` block below stops Postgres itself.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
from testcontainers.postgres import PostgresContainer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.clock  # noqa: E402
from app.db import make_sessionmaker  # noqa: E402
from app.domain.types import GpuUsage, ServerUsage  # noqa: E402
from app.integrations.grafana import Datasource, Series  # noqa: E402
from app.jobs.metrics import HEALTH_KEY  # noqa: E402
from app.models import (  # noqa: E402
    Booking, OutboxItem, Server, Session, TelegramTopic, User, WatchSubscription,
)
from app.services import audit, setup, usage  # noqa: E402
from app.settings_store import K, SettingsStore  # noqa: E402
from app.web.main import create_app  # noqa: E402

DEFAULT_OUT = Path(
    "/tmp/claude-1000/-mnt-macdisk-workspace-server-booking/"
    "1b685f4d-78ab-4b3e-907c-90fdfc60f581/scratchpad/previews"
)
NOW = datetime(2026, 11, 9, 10, 20, tzinfo=UTC)  # Mon 09, 11:20 Europe/Rome
CSS = (ROOT / "app/web/static/app.css").read_text()


class FakeGrafana:
    """Answers discovery queries with a few hosts; enough for the import page."""

    async def list_datasources(self):
        return [Datasource("prom1", "Prometheus", "prometheus")]

    async def instant_query(self, ds_uid, expr):
        if "node_cpu" in expr and "count" in expr:
            return [Series({"instance": f"{h}:9100"}, n) for h, n in (
                ("gpu-01.lab", 64), ("gpu-08.lab", 48), ("cpu-09.lab", 32), ("cpu-10.lab", 16))]
        if "count" in expr:
            return [Series({"instance": "gpu-01.lab:9400"}, 4),
                    Series({"instance": "gpu-08.lab:9400"}, 2)]
        return []


def _upgrade_head(url: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


MODELS = ("NVIDIA GeForce RTX 4080 SUPER", "NVIDIA GeForce RTX 5090", "NVIDIA GeForce RTX 4090")


def _snapshots(cpu, ram, gpus):
    return [ServerUsage(NOW - timedelta(minutes=m), cpu, ram, tuple(GpuUsage(*g, i, MODELS[len(gpus) % len(MODELS)])
                                                       for i, g in enumerate(gpus)))
            for m in range(55, -1, -5)]


async def seed(db, store) -> dict:
    users = {n: User(telegram_id=1000 + i, display_name=n, username=u, is_admin=(i == 0),
                     dm_blocked=(n == "Dario Neri"))
             for i, (n, u) in enumerate((("Ana Rossi", "ana"), ("Bob Bianchi", "bob"),
                                         ("Chiara Verdi", "chiara"), ("Dario Neri", None),
                                         ("Maria Francesca Delle Fontane", "mfdf")))}
    db.add_all(users.values())
    s = {
        "gpu-01": Server(name="gpu-01", hostname="gpu-01.lab", kind="grafana", has_gpu=True,
                         gpu_count=4),
        "gpu-02": Server(name="gpu-02", hostname="gpu-02.lab", kind="grafana", has_gpu=True,
                         gpu_count=2),
        "cpu-03": Server(name="cpu-03", hostname="cpu-03.lab", kind="grafana"),
        "cpu-04": Server(name="cpu-04", hostname="cpu-04.lab", kind="grafana"),
        "gpu-05": Server(name="gpu-05", hostname="gpu-05.lab", kind="grafana", has_gpu=True,
                         gpu_count=1),
        "legacy-06": Server(name="legacy-06", hostname="legacy-06.lab", kind="standalone"),
        "old-07": Server(name="old-07", hostname="old-07.lab", kind="grafana", enabled=False,
                         notes="Waiting for a new PSU."),
        # Grafana reports 2 GPUs: the import page offers to update the count.
        "gpu-08": Server(name="gpu-08", hostname="gpu-08.lab", kind="grafana", has_gpu=True,
                         gpu_count=1),
        "retired-09": Server(name="retired-09", hostname="retired-09.lab", kind="standalone",
                             enabled=False, archived_at=NOW - timedelta(days=3)),
    }
    db.add_all(s.values())
    await db.flush()
    u = users
    h = timedelta(hours=1)
    hour = NOW.replace(minute=0)
    bookings = [
        # gpu-01: one shareable booking alone, so others may join.
        Booking(user_id=u["Bob Bianchi"].id, server_id=s["gpu-01"].id, start_at=hour - 2 * h,
                end_at=hour + 5 * h, resource="cpu", shareable=True),
        # gpu-02: a shared pair (Chiara let others in; Dario joined on CPU only).
        Booking(user_id=u["Chiara Verdi"].id, server_id=s["gpu-02"].id, start_at=hour - 3 * h,
                end_at=hour + 2 * h, resource="cpu_gpu", shareable=True),
        Booking(user_id=u["Dario Neri"].id, server_id=s["gpu-02"].id, start_at=hour - h,
                end_at=hour + 26 * h, resource="cpu", is_shared=True),
        Booking(user_id=u["Dario Neri"].id, server_id=s["cpu-04"].id, start_at=hour + 2 * h,
                end_at=hour + 6 * h, resource="cpu"),
        Booking(user_id=u["Ana Rossi"].id, server_id=s["gpu-05"].id, start_at=hour - h,
                end_at=hour + 7 * h, resource="cpu_gpu", idle_warnings=2),
        # Ana's own shareable booking that Bob has joined (My bookings shows him).
        Booking(user_id=u["Ana Rossi"].id, server_id=s["cpu-03"].id, start_at=hour + 23 * h,
                end_at=hour + 27 * h, resource="cpu", shareable=True),
        Booking(user_id=u["Bob Bianchi"].id, server_id=s["cpu-03"].id, start_at=hour + 24 * h,
                end_at=hour + 26 * h, resource="cpu", is_shared=True),
        Booking(user_id=u["Ana Rossi"].id, server_id=s["gpu-01"].id, start_at=hour + 48 * h,
                end_at=hour + 52 * h, resource="cpu_gpu"),
        Booking(user_id=u["Bob Bianchi"].id, server_id=s["gpu-01"].id, start_at=hour + 26 * h,
                end_at=hour + 30 * h, resource="cpu"),
        # Long display name on a 1-GPU server, to check ellipsis on the card.
        Booking(user_id=u["Maria Francesca Delle Fontane"].id, server_id=s["gpu-08"].id,
                start_at=hour - h,
                end_at=hour + 30 * h, resource="cpu_gpu"),
        Booking(user_id=u["Chiara Verdi"].id, server_id=s["gpu-01"].id, start_at=hour + 72 * h,
                end_at=hour + 80 * h, resource="cpu_gpu"),
        Booking(user_id=u["Bob Bianchi"].id, server_id=s["cpu-03"].id, start_at=hour - 30 * h,
                end_at=hour - 26 * h, resource="cpu", status="cancelled",
                cancel_reason="maintenance", cancelled_at=hour - 31 * h),
        Booking(user_id=u["Chiara Verdi"].id, server_id=s["gpu-05"].id, start_at=hour - 50 * h,
                end_at=hour - 40 * h, resource="cpu_gpu", status="reclaimed"),
        Booking(user_id=u["Ana Rossi"].id, server_id=s["gpu-02"].id, start_at=hour - 80 * h,
                end_at=hour - 70 * h, resource="cpu_gpu", status="completed"),
        Booking(user_id=u["Bob Bianchi"].id, server_id=s["retired-09"].id,
                start_at=hour - 120 * h, end_at=hour - 110 * h, resource="cpu",
                status="completed"),
    ]
    db.add_all(bookings)
    for name, args in (
        ("gpu-01", (18.0, 31.0, [(35.0, 40.0), (28.0, 30.0), (0.0, 1.0), (0.0, 1.0)])),
        ("gpu-02", (86.0, 71.0, [(97.0, 82.0), (91.0, 76.0)])),
        ("cpu-03", (7.0, 23.0, [])),
        ("gpu-05", (2.0, 41.0, [(0.0, 12.0)])),
        ("gpu-08", (44.0, 63.0, [(88.0, 71.0)])),
    ):
        for snap in _snapshots(*args):
            await usage.save_snapshot(db, s[name].id, snap)
    db.add(WatchSubscription(user_id=u["Ana Rossi"].id, server_id=s["gpu-02"].id))
    db.add_all([
        TelegramTopic(thread_id=22, title="Machine booking", last_seen_at=NOW),
        TelegramTopic(thread_id=11, title="General", last_seen_at=NOW - h),
        OutboxItem(kind="tg_dm", payload={}, created_at=NOW, next_attempt_at=NOW),
        OutboxItem(kind="gcal_create", payload={}, created_at=NOW - 30 * h, attempts=9,
                   next_attempt_at=NOW, failed_at=NOW - 6 * h, last_error="calendar down"),
    ])
    await setup.complete_setup(store)
    for key, value in ((K.TG_BOT_USERNAME, "lab_booking_bot"), (K.TG_GROUP_ID, -100123),
                       (K.TG_GROUP_TITLE, "Robotics Lab"), (K.TG_TOPIC_ID, 22),
                       (K.GRAFANA_URL, "https://grafana.lab.example"),
                       (K.GRAFANA_USER, "booking"), (K.GRAFANA_DS_UID, "prom1"),
                       (K.PORTAL_BASE_URL, "https://servers.lab.example"),
                       ("grafana.datasources", [{"uid": "prom1", "name": "Prometheus"}])):
        await store.set(key, value)
    await store.set(K.TG_TOKEN, "123456:secret", secret=True)
    await store.set(K.GRAFANA_PASSWORD, "pw", secret=True)
    await store.set(K.WORKER_HEARTBEAT, {"at": (NOW - timedelta(seconds=20)).isoformat(),
                                         "bot_running": True, "bot_username": "lab_booking_bot"})
    await store.set(HEALTH_KEY, {"at": NOW.isoformat(), "ok": 4,
                                 "failed": {"cpu-04": "ConnectError: timed out after 10 s"},
                                 "last_ok_at": NOW.isoformat()})
    admin = u["Ana Rossi"]
    for i, (action, target, details) in enumerate((
        ("admin.server_create", "server:gpu-05", {"name": "gpu-05"}),
        ("booking.create", "booking:4", {"server": "gpu-05", "resource": "cpu_gpu"}),
        ("admin.rules", "settings:rules", {"max_booking_hours": 336}),
        ("booking.reclaim", "booking:10", {"idle_warnings": 3}),
        ("admin.grafana", "settings:grafana", {"url": "https://grafana.lab.example"}),
    )):
        await audit.record(db, action, target, now=NOW - timedelta(minutes=37 * (i + 1)),
                           details=details, actor_user_id=None if "reclaim" in action
                           else admin.id)
    sid = secrets.token_urlsafe(16)
    db.add(Session(id=sid, user_id=admin.id, created_at=NOW, expires_at=NOW + 30 * h))
    await db.commit()
    return {"sid": sid, "servers": {k: v.id for k, v in s.items()}, "bookings": bookings}


def inline(html: str) -> str:
    html = html.replace('<link rel="stylesheet" href="/static/app.css">', f"<style>{CSS}</style>")
    return re.sub(r'<script src="/static/htmx.min.js"></script>', "", html)


async def render(out: Path) -> list[str]:
    app.clock.utcnow = lambda: NOW
    with PostgresContainer("postgres:16") as pg:
        url = "postgresql+asyncpg://" + pg.get_connection_url().split("://", 1)[1]
        await asyncio.to_thread(_upgrade_head, url)
        engine = create_async_engine(url)
        sm = make_sessionmaker(engine)
        with tempfile.TemporaryDirectory() as data_dir:
            web = create_app(sm, data_dir=Path(data_dir))

            async def no_dns(name):
                raise OSError("DNS disabled in previews")

            web.state.resolver = no_dns
            web.state.grafana_factory = lambda *a: FakeGrafana()
            async with sm() as db:
                data = await seed(db, SettingsStore(db, web.state.box))
            ids = data["servers"]
            transport = httpx.ASGITransport(app=web)
            pages = {
                "board": "/", "book": f"/book?server_id={ids['gpu-01']}", "me": "/me",
                "admin_servers": "/admin/servers", "server_edit": f"/admin/servers/{ids['gpu-01']}",
                "server_new": "/admin/servers/new", "import": "/admin/servers/import",
                "integrations": "/admin/integrations", "rules": "/admin/rules",
                "bookings": "/admin/bookings", "health": "/admin/health", "audit": "/admin/audit",
                "setup_grafana": "/admin/setup/grafana", "setup_bind": "/admin/setup/bind",
                "servers_deleted": "/admin/servers?deleted=1",
                "server_delete": f"/admin/servers/{ids['gpu-01']}/delete",
                "server_purge": f"/admin/servers/{ids['retired-09']}/purge",
                "bookings_deleted": "/admin/bookings?deleted=1",
            }
            written = []
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
                r = await anon.get("/login")
                (out / "login.html").write_text(inline(r.text))
                written.append("login")
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                c.cookies.set("sb_session", web.state.signer.dumps(data["sid"]))
                for name, path in pages.items():
                    r = await c.get(path)
                    assert r.status_code == 200, (name, r.status_code, r.text[:300])
                    (out / f"{name}.html").write_text(inline(r.text))
                    written.append(name)
                # Share warning on the booking page.
                local = NOW.astimezone(__import__("zoneinfo").ZoneInfo("Europe/Rome"))
                start = local.replace(minute=0)
                r = await c.post("/book", data={
                    "server_id": str(ids["gpu-01"]), "start": f"{start:%Y-%m-%dT%H:%M}",
                    "end": f"{start + timedelta(hours=2):%Y-%m-%dT%H:%M}", "resource": "cpu"})
                (out / "book_share.html").write_text(inline(r.text))
                written.append("book_share")
                # Validation errors on the server form.
                r = await c.post("/admin/servers/new", data={
                    "name": "gpu-01", "hostname": "", "kind": "grafana", "has_gpu": "on",
                    "gpu_count": "0", "enabled": "on"})
                (out / "server_errors.html").write_text(inline(r.text))
                written.append("server_errors")
                # Success notice after "I'm still using it".
                async with sm() as db:
                    from sqlalchemy import select
                    bid = await db.scalar(select(Booking.id).where(Booking.idle_warnings > 0))
                r = await c.post(f"/bookings/{bid}/still-using")
                (out / "me_notice.html").write_text(inline(r.text))
                written.append("me_notice")
        await engine.dispose()
    return written


def screenshot(html: Path, png: Path, size: str) -> None:
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    f"--window-size={size}", f"--screenshot={png}", f"file://{html}"],
                   check=True, capture_output=True)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = Path(args[0]) if args else DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)
    written = asyncio.run(render(out))
    print("wrote", ", ".join(written))
    if "--shots" in sys.argv:
        for name in written:
            screenshot(out / f"{name}.html", out / f"{name}.png", "1280,900")
        screenshot(out / "board.html", out / "board-1280x800.png", "1280,800")
        # Headless Chrome lays out at least 500 px wide whatever --window-size says, so the
        # phone shots load the page in a 390 px iframe (its own viewport for media queries).
        for name in ("board", "book", "me"):
            frame = out / f"{name}-mobile-frame.html"
            frame.write_text(
                '<!DOCTYPE html><html><body style="margin:0">'
                f'<iframe src="{name}.html" style="width:390px;height:844px;border:0;display:block">'
                "</iframe></body></html>")
            screenshot(frame, out / f"{name}-mobile.png", "390,844")
        print("screenshots in", out)


if __name__ == "__main__":
    main()
