from collections.abc import Sequence
from datetime import datetime

from app.domain.status import LEGEND_TEXT, STATUS_EMOJI, format_day_hm
from app.domain.types import StatusCode
from app.services.board import ServerCard

FREE_CODES = (StatusCode.AVAILABLE, StatusCode.BOOKED_SOON, StatusCode.SHAREABLE)
DETAIL_NEXT = 3


def _line(card: ServerCard, tz: str) -> str:
    st = card.status
    parts = [f"{STATUS_EMOJI[st.code]} {card.server.name} — {st.label}"]
    if st.holders:
        parts.append(", ".join(st.holders))
    # BOOKED_SOON's label already reads "Booked from HH:MM".
    if st.next_busy is not None and st.code != StatusCode.BOOKED_SOON:
        parts.append(f"next: {format_day_hm(st.next_busy, tz)}")
    return " · ".join(parts)


def _footer(portal_url: str) -> list[str]:
    return [f"Book: {portal_url}"] if portal_url else []


def status_list(cards: Sequence[ServerCard], tz: str, portal_url: str) -> str:
    lines = [_line(c, tz) for c in cards]
    return "\n".join([*lines, "", LEGEND_TEXT, *_footer(portal_url)])


def free_list(cards: Sequence[ServerCard], tz: str, portal_url: str) -> str:
    free = [c for c in cards if c.status.code in FREE_CODES]
    if not free:
        return "\n".join(["No server is free right now.", *_footer(portal_url)])
    return status_list(free, tz, portal_url)


def _usage_line(card: ServerCard) -> str:
    if card.server.kind == "standalone":
        return "standalone — not monitored"
    u = card.usage
    if u is None or card.stale:
        return "usage: unknown"
    parts = [f"CPU {u.cpu_pct:.0f}%", f"RAM {u.ram_pct:.0f}%"]
    parts += [f"GPU{i} {g.util_pct:.0f}%/{g.mem_pct:.0f}%" for i, g in enumerate(u.gpus)]
    return " · ".join(parts)


def server_detail(card: ServerCard, tz: str, portal_url: str, now: datetime) -> str:
    lines = [_line(card, tz), _usage_line(card)]
    current = [b for b in card.upcoming if b.start <= now < b.end]
    later = [b for b in card.upcoming if b.start > now][:DETAIL_NEXT]
    for b in current:
        lines.append(f"Now: {b.user_name} until {format_day_hm(b.end, tz)}")
    if later:
        lines.append("Next:")
        lines += [
            f"• {b.user_name} {format_day_hm(b.start, tz)} → {format_day_hm(b.end, tz)}"
            for b in later
        ]
    elif not current:
        lines.append("No upcoming bookings.")
    return "\n".join([*lines, *_footer(portal_url)])


def idle_warning(server_name: str, n: int, max_n: int) -> str:
    return (
        f"⚠️ Your booking on {server_name} has been idle for 2 hours. "
        f"Warning {n}/{max_n} — after {max_n} warnings the booking is released."
    )
