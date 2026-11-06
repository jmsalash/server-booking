from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.domain.rules import Rules
from app.domain.types import BookingView, ServerInfo, ServerStatus, StatusCode

STATUS_EMOJI: dict[StatusCode, str] = {
    StatusCode.AVAILABLE: "🟢",
    StatusCode.BOOKED_SOON: "🟠",
    StatusCode.BOOKED: "🔴",
    StatusCode.FREE_SOON: "🔴➜🟢",
    StatusCode.SHAREABLE: "🟣",
    StatusCode.UNAVAILABLE: "⚪",
}

STATUS_LABEL: dict[StatusCode, str] = {
    StatusCode.AVAILABLE: "Available",
    StatusCode.BOOKED_SOON: "Booked soon",
    StatusCode.BOOKED: "Booked",
    StatusCode.FREE_SOON: "Free soon",
    StatusCode.SHAREABLE: "Shareable",
    StatusCode.UNAVAILABLE: "Unavailable",
}

LEGEND_TEXT = (
    "🟢 available · 🟠 booked soon · 🔴 booked · 🔴➜🟢 free soon · 🟣 shareable · ⚪ unavailable"
)

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _hm(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


def format_day_hm(dt: datetime, tz: str | ZoneInfo) -> str:
    """`ddd DD HH:MM` in the given timezone (fixed English day abbreviations)."""
    local = dt.astimezone(ZoneInfo(tz) if isinstance(tz, str) else tz)
    return f"{_DAYS[local.weekday()]} {local.day:02d} {local:%H:%M}"


def format_hm_or_day(dt: datetime, tz: str | ZoneInfo, now: datetime) -> str:
    """`HH:MM` when `dt` falls on the same local day as `now`, else `ddd DD HH:MM`."""
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    if dt.astimezone(zone).date() == now.astimezone(zone).date():
        return _hm(dt, zone)
    return format_day_hm(dt, zone)


def compute_status(
    server: ServerInfo,
    bookings: Sequence[BookingView],
    now: datetime,
    rules: Rules,
) -> ServerStatus:
    tz = ZoneInfo(rules.timezone)
    soon = timedelta(minutes=rules.soon_window_min)

    if not server.enabled:
        return ServerStatus(StatusCode.UNAVAILABLE, "Unavailable", None, None, ())

    ordered = sorted(bookings, key=lambda x: x.start)
    current = [x for x in ordered if x.start <= now < x.end]

    if current:
        holders = tuple(x.user_name for x in current)
        busy_until = max(x.end for x in current)
        extended = True
        while extended:
            extended = False
            for x in ordered:
                if x.start <= busy_until < x.end:
                    busy_until = x.end
                    extended = True
        next_busy = next((x.start for x in ordered if x.start > busy_until), None)
        # One person in, and they let one more join.
        if len(current) == 1 and current[0].shareable:
            code, label = StatusCode.SHAREABLE, "In use — can share"
        elif busy_until - now <= soon:
            code, label = StatusCode.FREE_SOON, f"Free soon (at {_hm(busy_until, tz)})"
        else:
            code, label = StatusCode.BOOKED, f"Booked until {format_day_hm(busy_until, tz)}"
        return ServerStatus(code, label, busy_until, next_busy, holders)

    next_busy = next((x.start for x in ordered if x.start > now), None)
    if next_busy is not None and next_busy - now <= soon:
        return ServerStatus(
            StatusCode.BOOKED_SOON, f"Booked from {_hm(next_busy, tz)}", None, next_busy, ()
        )
    return ServerStatus(StatusCode.AVAILABLE, "Available", None, next_busy, ())
