from datetime import timedelta

from app.domain.booking_rules import (
    BookingError as E,
    BookingRequest,
    validate_booking,
)
from app.domain.rules import Rules
from app.domain.timeutil import parse_local
from app.domain.types import BookingView, ServerInfo

R = Rules()
TZ = R.timezone
NOW = parse_local("2026-11-10T12:20", TZ)


def L(h: float):
    hh = int(h)
    return parse_local(f"2026-11-10T{hh:02d}:00", TZ)


def srv(shareable=False, has_gpu=True, enabled=True):
    return ServerInfo(1, "s1", "grafana", has_gpu, 1 if has_gpu else 0, shareable, enabled)


def req(s, e, resource="gpu"):
    return BookingRequest(L(s), L(e), resource)


def req_at(s: str, e: str, resource="gpu"):
    return BookingRequest(
        parse_local(f"2026-11-10T{s}", TZ), parse_local(f"2026-11-10T{e}", TZ), resource
    )


def req_days(s, days, extra_h=0, resource="gpu"):
    start = L(13 + s)
    return BookingRequest(start, start + timedelta(days=days, hours=extra_h), resource)


def b(s, e, user="bob", shared=False):
    return BookingView(1, 1, user, 1, L(s), L(e), "gpu", shared)


def v(r, existing=(), shareable=False, shareable_now=False, server=None):
    return validate_booking(
        r, list(existing), server or srv(shareable=shareable), shareable_now, NOW, R
    )


def test_ok_free_slot():
    assert v(req(13, 15)).ok


def test_not_hour_aligned():
    assert v(req_at("13:30", "15:00")).error == E.NOT_HOUR_ALIGNED
    assert v(req_at("13:00", "15:30")).error == E.NOT_HOUR_ALIGNED


def test_end_not_after_start():
    assert v(req(15, 15)).error == E.END_NOT_AFTER_START
    assert v(req(16, 15)).error == E.END_NOT_AFTER_START


def test_current_hour_start_allowed():
    assert v(req(12, 14)).ok


def test_past_rejected():
    assert v(req(11, 14)).error == E.IN_PAST


def test_max_14_days():
    assert v(req_days(0, 14)).ok
    assert v(req_days(0, 14, extra_h=1)).error == E.TOO_LONG


def test_gpu_on_cpu_server():
    r = v(req(13, 15, "cpu_gpu"), server=srv(has_gpu=False))
    assert r.error == E.NO_GPU


def test_server_disabled():
    assert v(req(13, 15), server=srv(enabled=False)).error == E.SERVER_DISABLED


def test_conflict_on_non_shareable():
    assert v(req(13, 15), existing=[b(10, 14)]).error == E.CONFLICT


def test_adjacent_is_not_overlap():
    assert v(req(14, 16), existing=[b(10, 14)]).ok


def test_shared_ok_when_shareable_now_and_starts_now():
    r = v(req(12, 16), existing=[b(10, 14, user="ana")], shareable=True, shareable_now=True)
    assert r.ok and r.is_shared and r.share_with == "ana"


def test_shared_future_rejected():
    r = v(req(13, 16), existing=[b(10, 14)], shareable=True, shareable_now=True)
    assert r.error == E.SHARE_MUST_START_NOW


def test_overlap_with_future_booking_while_shareable_rejected():
    r = v(req(12, 16), existing=[b(15, 18)], shareable=True, shareable_now=True)
    assert r.error == E.SHARE_MUST_START_NOW


def test_third_booking_rejected():
    r = v(
        req(12, 16),
        existing=[b(10, 14), b(12, 14, shared=True)],
        shareable=True,
        shareable_now=True,
    )
    assert r.error == E.TOO_MANY_OVERLAPS


def test_shareable_server_but_not_now_conflict():
    r = v(req(12, 16), existing=[b(10, 14)], shareable=True, shareable_now=False)
    assert r.error == E.CONFLICT
