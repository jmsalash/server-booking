from collections.abc import Sequence
from datetime import datetime

from app.domain.status import LEGEND_TEXT, STATUS_EMOJI, format_day_hm, format_hm_or_day
from app.domain.timeutil import duration_text  # noqa: F401 - re-exported
from app.domain.types import BookingView, DiskUsage, StatusCode
from app.services.board import ServerCard

FREE_CODES = (StatusCode.AVAILABLE, StatusCode.BOOKED_SOON, StatusCode.SHAREABLE)
DETAIL_NEXT = 3
CAN_JOIN = "🟣 One more person can join"


def _resource(b: BookingView) -> str:
    return "CPU + GPU" if b.resource == "cpu_gpu" else "CPU"


def _person(b: BookingView, tz: str, now: datetime | None) -> str:
    until = format_hm_or_day(b.end, tz, now) if now else format_day_hm(b.end, tz)
    return f"• {b.user_name} — {_resource(b)} — until {until}"


def _quiet(card: ServerCard, tz: str, now: datetime | None) -> list[str]:
    """A hint that a machine in use looks unused, so people can ask the booker."""
    if now is None or card.quiet_since is None or not card.current:
        return []
    minutes = int((now - card.quiet_since).total_seconds() // 60)
    since = format_hm_or_day(card.quiet_since, tz, now)
    return [f"💤 Quiet for {duration_text(minutes)} (no activity since {since})"]


def _line(card: ServerCard, tz: str) -> str:
    st = card.status
    parts = [f"{STATUS_EMOJI[st.code]} {card.server.name} — {st.label}"]
    if st.holders and not card.current:  # cards built without per-person details
        parts.append(", ".join(st.holders))
    # BOOKED_SOON's label already reads "Booked from HH:MM".
    if st.next_busy is not None and st.code != StatusCode.BOOKED_SOON:
        parts.append(f"next: {format_day_hm(st.next_busy, tz)}")
    return " · ".join(parts)


def _block(card: ServerCard, tz: str, now: datetime | None) -> list[str]:
    """The server's status line, then one line per person using it now."""
    lines = [_line(card, tz), *(_person(b, tz, now) for b in card.current),
             *_quiet(card, tz, now)]
    if card.status.code == StatusCode.SHAREABLE:
        lines.append(CAN_JOIN)
    return lines


def _footer(portal_url: str) -> list[str]:
    return [f"Book: {portal_url}"] if portal_url else []


def status_list(
    cards: Sequence[ServerCard], tz: str, portal_url: str, now: datetime | None = None
) -> str:
    lines = [line for c in cards for line in _block(c, tz, now)]
    return "\n".join([*lines, "", LEGEND_TEXT, *_footer(portal_url)])


def free_list(
    cards: Sequence[ServerCard], tz: str, portal_url: str, now: datetime | None = None
) -> str:
    free = [c for c in cards if c.status.code in FREE_CODES]
    if not free:
        return "\n".join(["No server is free right now.", *_footer(portal_url)])
    return status_list(free, tz, portal_url, now)


def _usage_line(card: ServerCard) -> str:
    if card.server.kind == "standalone":
        return "standalone — not monitored"
    u = card.usage
    if card.pending:
        return "usage: waiting for first reading"
    if u is None or card.stale:
        return "usage: unknown"
    parts = [f"CPU {u.cpu_pct:.0f}%", f"RAM {u.ram_pct:.0f}%"]
    parts += [f"{g.label(i, len(u.gpus))} {g.util_pct:.0f}% (mem {g.mem_pct:.0f}%)"
              for i, g in enumerate(u.gpus)]
    return " · ".join(parts)


def server_detail(card: ServerCard, tz: str, portal_url: str, now: datetime) -> str:
    current = card.current
    lines = [_line(card, tz), *(_person(b, tz, now) for b in current), *_quiet(card, tz, now)]
    if card.status.code == StatusCode.SHAREABLE:
        lines.append(CAN_JOIN)
    lines.append(_usage_line(card))
    later = [b for b in card.upcoming if b.start > now][:DETAIL_NEXT]
    if later:
        lines.append("Next:")
        lines += [
            f"• {b.user_name} {format_day_hm(b.start, tz)} → {format_day_hm(b.end, tz)}"
            for b in later
        ]
    elif not current:
        lines.append("No upcoming bookings.")
    return "\n".join([*lines, *_footer(portal_url)])


def _duration(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return "1 hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} minutes"


def idle_warning(server_name: str, n: int, max_n: int, idle_min: int) -> str:
    return (
        f"⚠️ Your booking on {server_name} has been idle for {_duration(idle_min)}. "
        f"Warning {n}/{max_n} — after {max_n} warnings the booking is released."
    )


def ram_full(server_name: str, pct: float) -> str:
    return f"⚠️ {server_name}: RAM is {pct:.0f}% full. Your job may crash if it runs out."


def disk_full(server_name: str, disks: Sequence[DiskUsage]) -> str:
    if len(disks) == 1:
        d = disks[0]
        return (f"⚠️ {server_name}: disk {d.mount} is {d.used_pct:.0f}% full. "
                "Free some space before it runs out.")
    listed = ", ".join(f"{d.mount} {d.used_pct:.0f}%" for d in disks)
    return (f"⚠️ {server_name}: disks are almost full: {listed}. "
            "Free some space before they run out.")
