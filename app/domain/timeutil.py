from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.domain.rules import Rules


def floor_hour(dt: datetime, tz: str) -> datetime:
    local = dt.astimezone(ZoneInfo(tz))
    return local.replace(minute=0, second=0, microsecond=0).astimezone(UTC)


def is_hour_aligned(dt: datetime, tz: str) -> bool:
    local = dt.astimezone(ZoneInfo(tz))
    return local.minute == 0 and local.second == 0 and local.microsecond == 0


def parse_local(value: str, tz: str) -> datetime:
    naive = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)


def _hm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def in_working_hours(dt: datetime, rules: Rules) -> bool:
    local = dt.astimezone(ZoneInfo(rules.timezone))
    if local.weekday() not in rules.work_days:
        return False
    return _hm(rules.work_start) <= local.time() < _hm(rules.work_end)


def working_minutes(start: datetime, end: datetime, rules: Rules) -> float:
    if end <= start:
        return 0.0
    tz = ZoneInfo(rules.timezone)
    ws, we = _hm(rules.work_start), _hm(rules.work_end)
    day = start.astimezone(tz).date()
    last = end.astimezone(tz).date()
    total = 0.0
    while day <= last:
        if day.weekday() in rules.work_days:
            w_start = datetime.combine(day, ws, tzinfo=tz).astimezone(UTC)
            w_end = datetime.combine(day, we, tzinfo=tz).astimezone(UTC)
            lo, hi = max(start, w_start), min(end, w_end)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60
        day += timedelta(days=1)
    return total


def duration_text(minutes: int) -> str:
    """`45 min`, `2 h`, `2 h 15 min`."""
    h, m = divmod(minutes, 60)
    if not h:
        return f"{m} min"
    return f"{h} h" if not m else f"{h} h {m} min"


def _work_spans(start: datetime, rules: Rules, days: int):
    """Working-hour intervals (UTC) from `start`'s local day on, clipped to start at `start`."""
    tz = ZoneInfo(rules.timezone)
    ws, we = _hm(rules.work_start), _hm(rules.work_end)
    day = start.astimezone(tz).date()
    for _ in range(days):
        if day.weekday() in rules.work_days:
            lo = datetime.combine(day, ws, tzinfo=tz).astimezone(UTC)
            hi = datetime.combine(day, we, tzinfo=tz).astimezone(UTC)
            if hi > start:
                yield max(lo, start), hi
        day += timedelta(days=1)


def add_working_minutes(start: datetime, minutes: float, rules: Rules) -> datetime | None:
    """The first instant inside working hours by which `minutes` of working time have passed
    since `start` (None if there are no working days)."""
    left = minutes
    for lo, hi in _work_spans(start, rules, days=15):
        span = (hi - lo).total_seconds() / 60
        if left < span:
            return lo + timedelta(minutes=left)
        left -= span
    return None


def next_work_start(dt: datetime, rules: Rules) -> datetime | None:
    """`dt` if it is inside working hours, else when working hours next begin."""
    return next((lo for lo, _hi in _work_spans(dt, rules, days=15)), None)
