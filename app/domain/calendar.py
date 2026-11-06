"""Week calendar layout: where days, working hours and bookings sit on a Mon–Sun track.

Positions are percentages of the week's real elapsed time, so a week with a DST change
(167 or 169 hours) still lines bookings up with the day separators.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.domain.rules import Rules
from app.domain.status import format_day_hm

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _midnight(d: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(d, time(0), tzinfo=zone).astimezone(UTC)


@dataclass(frozen=True)
class Week:
    monday: date
    start: datetime  # Monday 00:00 local, as UTC
    end: datetime  # the next Monday 00:00 local, as UTC
    tz: str

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600

    @property
    def prev(self) -> date:
        return self.monday - timedelta(days=7)

    @property
    def next(self) -> date:
        return self.monday + timedelta(days=7)

    def pct(self, dt: datetime) -> float:
        """Where `dt` falls in the week, 0–100, clamped to the week."""
        total = (self.end - self.start).total_seconds()
        x = (dt - self.start).total_seconds() / total * 100
        return min(100.0, max(0.0, x))

    def contains(self, dt: datetime) -> bool:
        return self.start <= dt < self.end


def week_containing(d: date, tz: str) -> Week:
    zone = ZoneInfo(tz)
    monday = d - timedelta(days=d.weekday())
    return Week(monday, _midnight(monday, zone), _midnight(monday + timedelta(days=7), zone), tz)


def week_of(now: datetime, tz: str) -> Week:
    return week_containing(now.astimezone(ZoneInfo(tz)).date(), tz)


def range_label(week: Week) -> str:
    """`10 – 16 Nov 2026`, `28 Sep – 4 Oct 2026`, `29 Dec 2025 – 4 Jan 2026`."""
    a, b = week.monday, week.monday + timedelta(days=6)
    if a.year != b.year:
        return f"{a.day} {_MONTHS[a.month - 1]} {a.year} – {b.day} {_MONTHS[b.month - 1]} {b.year}"
    if a.month != b.month:
        return f"{a.day} {_MONTHS[a.month - 1]} – {b.day} {_MONTHS[b.month - 1]} {b.year}"
    return f"{a.day} – {b.day} {_MONTHS[b.month - 1]} {b.year}"


@dataclass(frozen=True)
class Span:
    left: float  # percent of the week
    width: float


@dataclass(frozen=True)
class DayColumn(Span):
    day: date = date.min
    is_today: bool = False

    @property
    def label(self) -> str:
        return f"{_DAYS[self.day.weekday()]} {self.day.day}"


def day_columns(week: Week, now: datetime) -> list[DayColumn]:
    zone = ZoneInfo(week.tz)
    today = now.astimezone(zone).date()
    out = []
    for i in range(7):
        d = week.monday + timedelta(days=i)
        lo = week.pct(_midnight(d, zone))
        hi = week.pct(_midnight(d + timedelta(days=1), zone))
        out.append(DayColumn(lo, hi - lo, d, d == today))
    return out


def _hm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def work_bands(week: Week, rules: Rules) -> list[Span]:
    """Working hours on working days, as spans of the week."""
    zone = ZoneInfo(week.tz)
    ws, we = _hm(rules.work_start), _hm(rules.work_end)
    out = []
    for i in range(7):
        d = week.monday + timedelta(days=i)
        if d.weekday() not in rules.work_days:
            continue
        lo = week.pct(datetime.combine(d, ws, tzinfo=zone).astimezone(UTC))
        hi = week.pct(datetime.combine(d, we, tzinfo=zone).astimezone(UTC))
        if hi > lo:
            out.append(Span(lo, hi - lo))
    return out


def now_pct(week: Week, now: datetime) -> float | None:
    return week.pct(now) if week.contains(now) else None


# Bar widths (percent of the week) below which the label shortens; a day is ~14.3%.
FULL_NAME_PCT = 10.0
INITIALS_PCT = 4.0


@dataclass(frozen=True)
class Bar(Span):
    """One booking drawn on a machine's row. Lane 0: the booking; lane 1: a joined partner."""
    booking: object = None  # BookingView
    lane: int = 0
    from_before: bool = False  # started in an earlier week
    into_after: bool = False  # continues into a later week
    shared_with: tuple[str, ...] = ()
    past: bool = False  # already over

    @property
    def label(self) -> str:
        """What fits on the bar: the full name, the first name, or initials (a few hours)."""
        name = self.booking.user_name.strip()
        words = name.split()
        if self.width >= FULL_NAME_PCT or len(words) < 2:
            return name if self.width >= INITIALS_PCT or not words else words[0][:2]
        if self.width >= INITIALS_PCT:
            return words[0]
        return "".join(w[0] for w in words[:2]).upper()

    def title(self, tz: str) -> str:
        b = self.booking
        parts = [b.user_name, "CPU + GPU" if b.resource == "cpu_gpu" else "CPU",
                 f"{format_day_hm(b.start, tz)} – {format_day_hm(b.end, tz)}"]
        if self.shared_with:
            parts.append("shared with " + ", ".join(self.shared_with))
        elif b.shareable:
            parts.append("shareable")
        return " · ".join(parts)


def layout_bars(bookings: Sequence, week: Week, now: datetime) -> list[Bar]:
    """Bars for one machine's bookings (BookingViews) that touch the week."""
    out = []
    for b in bookings:
        if b.end <= week.start or b.start >= week.end:
            continue
        left = week.pct(b.start)
        partners = tuple(x.user_name for x in bookings
                         if x.id != b.id and x.start < b.end and b.start < x.end)
        out.append(Bar(
            round(left, 3), round(week.pct(b.end) - left, 3), b,
            lane=1 if b.is_shared else 0,
            from_before=b.start < week.start, into_after=b.end > week.end,
            shared_with=partners, past=b.end <= now,
        ))
    return out
