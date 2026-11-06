from datetime import datetime, timedelta, timezone

from app.bot.format import free_list, idle_warning, server_detail, status_list
from app.domain.status import LEGEND_TEXT
from app.domain.types import (
    BookingView, GpuUsage, ServerInfo, ServerStatus, ServerUsage, StatusCode,
)
from app.services.board import ServerCard

TZ = "Europe/Rome"
URL = "https://book.lab"
NOW = datetime(2026, 11, 5, 11, 0, tzinfo=timezone.utc)  # Thu 05 12:00 Rome


def bv(user, start, hours, bid=1):
    return BookingView(bid, 1, user, 1, start, start + timedelta(hours=hours), "cpu", False)


def card(code, name="gpu-01", label=None, holders=(), next_busy=None, kind="grafana",
         usage=None, stale=False, upcoming=(), pending=False):
    info = ServerInfo(1, name, kind, True, 2, True, code != StatusCode.UNAVAILABLE)
    status = ServerStatus(code, label or code.value, None, next_busy, tuple(holders))
    return ServerCard(info, status, usage, stale, list(upcoming), pending)


def test_status_list_has_emoji_legend_and_link():
    text = status_list(
        [
            card(StatusCode.BOOKED, label="Booked until Thu 05 18:00", holders=["Ana", "Bo"],
                 next_busy=NOW + timedelta(days=1)),
            card(StatusCode.AVAILABLE, name="cpu-02", label="Available"),
        ],
        TZ, URL,
    )
    lines = text.split("\n")
    assert lines[0] == "🔴 gpu-01 — Booked until Thu 05 18:00 · Ana, Bo · next: Fri 06 12:00"
    assert lines[1] == "🟢 cpu-02 — Available"
    assert lines[2] == ""
    assert lines[3] == LEGEND_TEXT
    assert lines[4] == f"Book: {URL}"


def test_status_list_without_portal_omits_book_line():
    text = status_list([card(StatusCode.AVAILABLE, label="Available")], TZ, "")
    assert text.endswith(LEGEND_TEXT)


def test_free_soon_uses_red_arrow_green():
    assert "🔴➜🟢" in status_list([card(StatusCode.FREE_SOON)], TZ, URL)


def test_free_list_only_free_kinds():
    cards = [card(c, name=c.value) for c in StatusCode]
    text = free_list(cards, TZ, URL)
    for c in (StatusCode.AVAILABLE, StatusCode.BOOKED_SOON, StatusCode.SHAREABLE):
        assert f" {c.value} — " in text
    for c in (StatusCode.BOOKED, StatusCode.FREE_SOON, StatusCode.UNAVAILABLE):
        assert f" {c.value} — " not in text
    assert f"Book: {URL}" in text


def test_free_list_empty_message():
    text = free_list([card(StatusCode.BOOKED)], TZ, URL)
    assert text.startswith("No server is free right now.")
    assert "gpu-01" not in text


def test_detail_unknown_usage_when_stale():
    u = ServerUsage(NOW, 34.0, 51.0, (GpuUsage(90.0, 80.0),))
    text = server_detail(card(StatusCode.AVAILABLE, usage=u, stale=True), TZ, URL, NOW)
    assert "usage: unknown" in text and "CPU" not in text
    assert "usage: unknown" in server_detail(card(StatusCode.AVAILABLE), TZ, URL, NOW)


def test_detail_waiting_for_first_reading():
    text = server_detail(card(StatusCode.AVAILABLE, stale=True, pending=True), TZ, URL, NOW)
    assert "waiting for first reading" in text


def test_detail_usage_line():
    u = ServerUsage(NOW, 34.2, 51.0, (GpuUsage(90.0, 80.4), GpuUsage(0.0, 1.0)))
    text = server_detail(card(StatusCode.AVAILABLE, usage=u), TZ, URL, NOW)
    assert "CPU 34% · RAM 51% · GPU0 90%/80% · GPU1 0%/1%" in text


def test_detail_standalone_not_monitored():
    text = server_detail(card(StatusCode.AVAILABLE, kind="standalone"), TZ, URL, NOW)
    assert "standalone — not monitored" in text


def test_detail_current_and_next_three_bookings():
    up = [bv("Ana", NOW - timedelta(hours=1), 2, 1)] + [
        bv(f"U{i}", NOW + timedelta(days=i), 1, i + 1) for i in range(1, 5)
    ]
    text = server_detail(card(StatusCode.BOOKED, upcoming=up), TZ, URL, NOW)
    assert "Now: Ana until Thu 05 13:00" in text
    assert "U1 Fri 06 12:00 → Fri 06 13:00" in text
    assert "U3" in text and "U4" not in text
    assert text.endswith(f"Book: {URL}")


def test_idle_warning_text():
    assert idle_warning("gpu-01", 2, 3, 120) == (
        "⚠️ Your booking on gpu-01 has been idle for 2 hours. Warning 2/3 — "
        "after 3 warnings the booking is released."
    )
    assert "idle for 1 hour. " in idle_warning("gpu-01", 1, 3, 60)
    assert "idle for 90 minutes. " in idle_warning("gpu-01", 1, 3, 90)
