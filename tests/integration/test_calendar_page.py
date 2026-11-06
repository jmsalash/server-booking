import re
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

import app.clock
from app.domain.types import GpuUsage, ServerUsage
from app.services import usage
from tests.integration.test_portal import _add, login_as

NOW = datetime(2026, 11, 9, 10, 20, tzinfo=UTC)  # Mon 09, 11:20 Europe/Rome
H = timedelta(hours=1)


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    monkeypatch.setattr(app.clock, "utcnow", lambda: NOW)


@pytest_asyncio.fixture
async def ana(db, client, web_app, make_user):
    user = await make_user(display_name="Ana Rossi")
    await login_as(db, client, web_app, user)
    return user


def _bar(html: str, name: str) -> str:
    """The opening tag of the bar booked by `name` (its title starts with the full name)."""
    m = re.search(rf'<(?:a|span) class="cal-bar [^>]*title="{re.escape(name)} ·[^>]*>', html)
    assert m, f"no bar for {name}"
    return m.group(0)


async def test_calendar_requires_login(client):
    r = await client.get("/calendar")
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_calendar_rejects_inactive_user(client, db, web_app, make_user):
    user = await make_user(display_name="Gone", active=False)
    await login_as(db, client, web_app, user)
    r = await client.get("/calendar")
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_calendar_shows_this_week(client, ana, db, make_user, make_server):
    bob = await make_user(display_name="Bob Bianchi")
    gpu = await make_server(name="gpu-01", kind="grafana", has_gpu=True, gpu_count=2)
    await make_server(name="cpu-02")
    await usage.save_snapshot(db, gpu.id, ServerUsage(NOW, 1.0, 1.0, (
        GpuUsage(0, 0, 0, "NVIDIA GeForce RTX 4090"), GpuUsage(0, 0, 1, "NVIDIA GeForce RTX 4090"))))
    await _add(db, bob, gpu, NOW + 2 * H, NOW + 6 * H, resource="cpu_gpu")
    await db.commit()
    r = await client.get("/calendar")
    assert r.status_code == 200
    assert '<a href="/calendar" aria-current="page">Calendar</a>' in r.text
    assert "9 – 15 Nov 2026" in r.text
    assert "/calendar?week=2026-11-02" in r.text and "/calendar?week=2026-11-16" in r.text
    assert "cal__day--today" in r.text and "Mon 9" in r.text
    assert 'class="cal__now"' in r.text
    assert r.text.count('class="cal__work"') == 5
    assert f'href="/book?server_id={gpu.id}"' in r.text
    assert "2× RTX 4090" in r.text and "CPU only" in r.text
    bar = _bar(r.text, "Bob Bianchi")
    assert bar.startswith("<span") and "cal-bar--lane0" in bar and "cal-bar--mine" not in bar
    assert "Bob Bianchi · CPU + GPU · Mon 09 13:20 – Mon 09 17:20" in bar


async def test_calendar_week_param(client, ana, db, make_user, make_server):
    server = await make_server(name="srv")
    await _add(db, ana, server, NOW + 7 * 24 * H, NOW + 7 * 24 * H + 3 * H)
    await db.commit()
    r = await client.get("/calendar")
    assert 'title="Ana Rossi ·' not in r.text
    r = await client.get("/calendar?week=2026-11-19")  # any day of that week
    assert r.status_code == 200 and "16 – 22 Nov 2026" in r.text
    assert "cal__day--today" not in r.text and 'class="cal__now"' not in r.text
    assert 'title="Ana Rossi ·' in r.text
    for bad in ("nonsense", "1900-01-01", "2026-13-01"):
        r = await client.get(f"/calendar?week={bad}")
        assert r.status_code == 200 and "9 – 15 Nov 2026" in r.text


async def test_own_booking_links_to_me(client, ana, db, make_server):
    server = await make_server(name="srv")
    await _add(db, ana, server, NOW - H, NOW + 3 * H, shareable=True)
    await db.commit()
    r = await client.get("/calendar")
    bar = _bar(r.text, "Ana Rossi")
    assert bar.startswith("<a") and 'href="/me"' in bar
    assert "cal-bar--mine" in bar and "cal-bar--shareable" in bar and "· shareable" in bar


async def test_partner_in_second_lane(client, ana, db, make_user, make_server):
    chiara = await make_user(display_name="Chiara")
    dario = await make_user(display_name="Dario")
    server = await make_server(name="srv")
    await _add(db, chiara, server, NOW - 2 * H, NOW + 4 * H, shareable=True)
    await _add(db, dario, server, NOW - H, NOW + 2 * H, is_shared=True)
    await db.commit()
    r = await client.get("/calendar")
    host, partner = _bar(r.text, "Chiara"), _bar(r.text, "Dario")
    assert "cal-bar--lane0" in host and "shared with Dario" in host
    assert "cal-bar--lane1" in partner and "shared with Chiara" in partner


async def test_hidden_and_greyed_rows(client, ana, db, make_user, make_server):
    bob = await make_user(display_name="Bob")
    gone = await make_server(name="retired-9", archived_at=NOW - 24 * H)
    off = await make_server(name="old-7", enabled=False)
    live = await make_server(name="live-1")
    await _add(db, bob, gone, NOW - 3 * H, NOW - 2 * H, status="completed")
    await _add(db, bob, live, NOW + H, NOW + 2 * H, status="cancelled")
    await _add(db, bob, live, NOW - 5 * H, NOW - 4 * H, status="reclaimed")
    await _add(db, bob, live, NOW - 8 * H, NOW - 7 * H, status="completed")
    await db.commit()
    r = await client.get("/calendar")
    assert "retired-9" not in r.text
    assert "cal__row--off" in r.text and "old-7" in r.text and "Unavailable" in r.text
    assert f"/book?server_id={off.id}" not in r.text
    # Only the completed booking is drawn, as a past one.
    assert r.text.count('title="Bob ·') == 1
    assert "cal-bar--past" in _bar(r.text, "Bob")


async def test_booking_across_weeks_is_clipped(client, ana, db, make_user, make_server):
    bob = await make_user(display_name="Bob")
    server = await make_server(name="srv")
    await _add(db, bob, server, NOW - 3 * 24 * H, NOW + H)  # from Fri 06
    await db.commit()
    bar = _bar((await client.get("/calendar")).text, "Bob")
    assert "cal-bar--from-before" in bar and "cal-bar--into-after" not in bar
    assert "left: 0.000%" in bar


async def test_about_links_calendar(client, ana):
    r = await client.get("/about")
    assert 'on the <a href="/calendar">Calendar</a>' in r.text
