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
