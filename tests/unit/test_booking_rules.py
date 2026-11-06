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


def srv(has_gpu=True, enabled=True):
    return ServerInfo(1, "s1", "grafana", has_gpu, 1 if has_gpu else 0, enabled)


def req(s, e, resource="gpu", user_id=None):
    return BookingRequest(L(s), L(e), resource, user_id)


def req_at(s: str, e: str, resource="gpu"):
    return BookingRequest(
        parse_local(f"2026-11-10T{s}", TZ), parse_local(f"2026-11-10T{e}", TZ), resource
    )


def req_days(s, days, extra_h=0, resource="gpu"):
    start = L(13 + s)
    return BookingRequest(start, start + timedelta(days=days, hours=extra_h), resource)


_ids = iter(range(1, 10_000))


def b(s, e, user="bob", shared=False, shareable=False, user_id=1):
    return BookingView(next(_ids), user_id, user, 1, L(s), L(e), "gpu", shared,
                       shareable=shareable)


def v(r, existing=(), server=None, paired=frozenset(), may_join=True):
    return validate_booking(r, list(existing), server or srv(), NOW, R, paired, may_join)


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


def test_join_shareable_booking():
    ana = b(10, 14, user="ana", shareable=True)
    r = v(req(12, 14), existing=[ana])
    assert r.ok and r.is_shared and r.share_with == (ana,)


def test_join_must_end_by_the_main_booking_end():
    ana = b(10, 14, user="ana", shareable=True)
    r = v(req(12, 16), existing=[ana])
    assert r.error == E.OUTSIDE_HOST and r.host == ana and not r.is_shared


def test_join_must_not_start_before_the_main_booking():
    ana = b(14, 20, user="ana", shareable=True)
    r = v(req(13, 16), existing=[ana])
    assert r.error == E.OUTSIDE_HOST and r.host == ana


def test_join_future_shareable_booking():
    r = v(req(15, 17), existing=[b(14, 20, shareable=True)])
    assert r.ok and r.is_shared


def test_cannot_join_two_consecutive_shareable_bookings():
    # Borrowed time lies inside one main booking, so it can't span two.
    x, y = b(10, 14, shareable=True), b(14, 18, shareable=True)
    r = v(req(13, 15), existing=[x, y])
    assert r.error == E.OUTSIDE_HOST and r.host == x


def test_extension_never_joins():
    r = v(req(13, 15), existing=[b(14, 18, shareable=True)], may_join=False)
    assert r.error == E.CONFLICT


def test_overlap_shareable_and_non_shareable_is_conflict():
    r = v(req(13, 15), existing=[b(10, 14, shareable=True), b(14, 18)])
    assert r.error == E.CONFLICT


def test_third_person_refused():
    r = v(req(12, 16), existing=[b(10, 14, shareable=True), b(12, 14, shared=True)])
    assert r.error == E.TOO_MANY_OVERLAPS


def test_partner_elsewhere_in_shareable_booking_does_not_block():
    # Ana 10-18 shares 10-12 with Bob; joining 14-16 makes only two people at any time.
    r = v(req(14, 16), existing=[b(10, 18, shareable=True), b(10, 12, shared=True)])
    assert r.ok and r.is_shared


def test_cannot_join_own_booking():
    r = v(req(12, 16, user_id=7), existing=[b(10, 14, shareable=True, user_id=7)])
    assert r.error == E.CONFLICT


def test_paired_booking_need_not_be_shareable():
    partner = b(12, 16, shared=True)
    r = v(req(12, 18), existing=[partner], paired=frozenset({partner.id}))
    assert r.ok and r.is_shared and r.share_with == ()


def test_paired_still_counts_towards_two():
    partner = b(12, 18, shared=True)
    r = v(req(12, 18), existing=[partner, b(15, 18, shareable=True)],
          paired=frozenset({partner.id}))
    assert r.error == E.TOO_MANY_OVERLAPS
