from datetime import timedelta

from app.domain.rules import Rules
from app.domain.status import LEGEND_TEXT, STATUS_EMOJI, STATUS_LABEL, compute_status
from app.domain.timeutil import parse_local
from app.domain.types import BookingView, ServerInfo, StatusCode

TZ = "Europe/Rome"


def L(s):
    return parse_local(s, TZ)


NOW = L("2026-11-10T12:00")
RULES = Rules()


def server(enabled=True):
    return ServerInfo(1, "srv", "standalone", False, 0, enabled)


def b(sh, eh, shared=False, user="Ann", shareable=False):
    return BookingView(
        1, 1, user, 1, L(f"2026-11-10T{sh:02d}:00"), L(f"2026-11-10T{eh:02d}:00"), "all", shared,
        shareable=shareable,
    )


def st(bookings=(), enabled=True, now=NOW):
    return compute_status(server(enabled), list(bookings), now, RULES)


def test_disabled_is_unavailable():
    s = st([b(10, 14)], enabled=False)
    assert s.code == StatusCode.UNAVAILABLE and s.label == "Unavailable"


def test_free_no_bookings_available():
    s = st([])
    assert s.code == StatusCode.AVAILABLE and s.label == "Available"
    assert s.busy_until is None and s.next_busy is None and s.holders == ()


def test_free_booking_in_2h_booked_soon():
    s = st([b(14, 18)])
    assert s.code == StatusCode.BOOKED_SOON and s.next_busy == L("2026-11-10T14:00")
    assert s.label == "Booked from 14:00"


def test_booked_soon_inclusive_boundary():
    assert st([b(15, 18)]).code == StatusCode.BOOKED_SOON


def test_free_booking_in_4h_available_with_next_busy():
    s = st([b(16, 18)])
    assert s.code == StatusCode.AVAILABLE and s.next_busy == L("2026-11-10T16:00")


def test_booked_ending_in_2h_free_soon():
    s = st([b(10, 14)])
    assert s.code == StatusCode.FREE_SOON and s.busy_until == L("2026-11-10T14:00")
    assert s.label == "Free soon (at 14:00)" and s.holders == ("Ann",)


def test_back_to_back_merged_is_booked():
    s = st([b(10, 14), b(14, 20, user="Bob")])
    assert s.code == StatusCode.BOOKED and s.busy_until == L("2026-11-10T20:00")
    assert s.label == "Booked until Tue 10 20:00"
    assert s.holders == ("Ann",)


def test_overlapping_chain_merged():
    s = st([b(10, 14), b(13, 20)])
    assert s.busy_until == L("2026-11-10T20:00")


def test_booked_long_is_booked():
    s = st([b(10, 20)])
    assert s.code == StatusCode.BOOKED and s.next_busy is None


def test_booked_next_busy_after_gap():
    s = st([b(10, 20), L_b(21, 22)])
    assert s.next_busy == L("2026-11-10T21:00")


def L_b(sh, eh):
    return b(sh, eh)


def test_one_shareable_booking_is_shareable():
    s = st([b(10, 20, shareable=True)])
    assert s.code == StatusCode.SHAREABLE and s.label == "In use — can share"


def test_shareable_wins_over_free_soon():
    assert st([b(10, 13, shareable=True)]).code == StatusCode.SHAREABLE


def test_one_non_shareable_booking_is_booked():
    assert st([b(10, 20)]).code == StatusCode.BOOKED


def test_two_people_is_booked():
    s = st([b(10, 20, shareable=True), b(11, 20, shared=True, user="Bob")])
    assert s.code == StatusCode.BOOKED and s.holders == ("Ann", "Bob")


def test_future_shareable_booking_is_booked_soon():
    assert st([b(13, 20, shareable=True)]).code == StatusCode.BOOKED_SOON


def test_booking_ending_exactly_now_is_not_current():
    s = st([b(10, 12)])
    assert s.code == StatusCode.AVAILABLE


def test_booking_starting_exactly_now_is_current():
    assert st([b(12, 20)]).code == StatusCode.BOOKED


def test_legend_and_maps():
    assert set(STATUS_EMOJI) == set(StatusCode) == set(STATUS_LABEL)
    assert LEGEND_TEXT == "🟢 available · 🟠 booked soon · 🔴 booked · 🔴➜🟢 free soon · 🟣 shareable · ⚪ unavailable"


def test_hm_or_day():
    from app.domain.status import format_hm_or_day

    assert format_hm_or_day(L("2026-11-10T18:00"), TZ, NOW) == "18:00"
    assert format_hm_or_day(L("2026-11-11T09:00"), TZ, NOW) == "Wed 11 09:00"
