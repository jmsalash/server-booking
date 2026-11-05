from datetime import UTC, datetime

from app.domain.rules import Rules
from app.domain.timeutil import (
    floor_hour,
    in_working_hours,
    is_hour_aligned,
    parse_local,
    working_minutes,
)

TZ = "Europe/Rome"


def L(s):
    return parse_local(s, TZ)


def test_parse_local_rome_winter():
    assert parse_local("2026-11-10T14:00", TZ) == datetime(2026, 11, 10, 13, tzinfo=UTC)


def test_parse_local_rome_summer():
    assert parse_local("2026-07-10T14:00", TZ) == datetime(2026, 7, 10, 12, tzinfo=UTC)


def test_working_minutes_spans_night():
    assert working_minutes(L("2026-11-10T18:00"), L("2026-11-11T10:00"), Rules()) == 120


def test_working_minutes_weekend_zero():
    assert working_minutes(L("2026-11-14T10:00"), L("2026-11-15T18:00"), Rules()) == 0


def test_working_minutes_across_dst_end():
    assert working_minutes(L("2026-10-23T18:00"), L("2026-10-26T10:00"), Rules()) == 120


def test_working_minutes_end_before_start_is_zero():
    assert working_minutes(L("2026-11-11T10:00"), L("2026-11-10T10:00"), Rules()) == 0


def test_working_minutes_full_day():
    assert working_minutes(L("2026-11-10T00:00"), L("2026-11-11T00:00"), Rules()) == 600


def test_floor_hour():
    assert floor_hour(L("2026-11-10T14:37"), TZ) == L("2026-11-10T14:00")


def test_is_hour_aligned():
    assert is_hour_aligned(L("2026-11-10T14:00"), TZ)
    assert not is_hour_aligned(L("2026-11-10T14:30"), TZ)


def test_in_working_hours():
    r = Rules()
    assert in_working_hours(L("2026-11-10T09:00"), r)
    assert not in_working_hours(L("2026-11-10T19:00"), r)
    assert not in_working_hours(L("2026-11-14T10:00"), r)
