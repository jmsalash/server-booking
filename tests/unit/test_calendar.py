from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain.calendar import (
    day_columns,
    layout_bars,
    now_pct,
    range_label,
    week_containing,
    week_of,
    work_bands,
)
from app.domain.rules import Rules
from app.domain.timeutil import parse_local
from app.domain.types import BookingView

TZ = "Europe/Rome"


def L(s):
    return parse_local(s, TZ)


def bv(id, start, end, is_shared=False, shareable=False, name=None, resource="cpu"):
    return BookingView(id=id, user_id=id, user_name=name or f"User {id}", server_id=1,
                       start=start, end=end, resource=resource, is_shared=is_shared,
                       shareable=shareable)


def test_week_runs_monday_to_monday_local():
    w = week_containing(date(2026, 11, 12), TZ)  # a Thursday
    assert w.monday == date(2026, 11, 9)
    assert w.start == L("2026-11-09T00:00") and w.end == L("2026-11-16T00:00")
    assert w.hours == 168 and w.prev == date(2026, 11, 2) and w.next == date(2026, 11, 16)


def test_week_of_uses_local_date():
    # Sunday 23:30 UTC is already Monday in Rome.
    w = week_of(datetime(2026, 11, 15, 23, 30, tzinfo=UTC), TZ)
    assert w.monday == date(2026, 11, 16)


@pytest.mark.parametrize("day, hours", [(date(2026, 10, 25), 169), (date(2026, 3, 29), 167)])
def test_dst_weeks_use_real_hours(day, hours):
    w = week_containing(day, TZ)
    assert w.hours == hours
    cols = day_columns(w, w.start)
    assert cols[0].left == 0 and abs(sum(c.width for c in cols) - 100) < 1e-9
    # The DST Sunday is 25 h (or 23 h) wide; the other days 24 h.
    assert cols[6].width == pytest.approx((hours - 144) / hours * 100)
    assert cols[0].width == pytest.approx(24 / hours * 100)


def test_dst_week_positions_by_elapsed_time():
    w = week_containing(date(2026, 10, 25), TZ)  # clocks go back Sun 25 Oct 03:00
    # Sunday 12:00 local is 6 days + 13 real hours into the week.
    assert w.pct(L("2026-10-25T12:00")) == pytest.approx((6 * 24 + 13) / 169 * 100)


def test_day_columns_mark_today():
    w = week_containing(date(2026, 11, 9), TZ)
    cols = day_columns(w, L("2026-11-11T08:00"))
    assert [c.is_today for c in cols] == [False, False, True, False, False, False, False]
    assert cols[2].label == "Wed 11"


def test_work_bands_only_on_work_days():
    w = week_containing(date(2026, 11, 9), TZ)
    bands = work_bands(w, Rules(work_start="09:00", work_end="19:00"))
    assert len(bands) == 5
    assert bands[0].left == pytest.approx(9 / 168 * 100)
    assert bands[0].width == pytest.approx(10 / 168 * 100)


def test_now_pct_only_inside_the_week():
    w = week_containing(date(2026, 11, 9), TZ)
    assert now_pct(w, L("2026-11-10T00:00")) == pytest.approx(100 / 7)
    assert now_pct(w, L("2026-11-16T00:00")) is None


def test_bars_clip_to_the_week_with_edge_flags():
    w = week_containing(date(2026, 11, 9), TZ)
    now = L("2026-11-09T12:00")
    inside = bv(1, L("2026-11-10T10:00"), L("2026-11-10T14:00"))
    before = bv(2, L("2026-11-07T10:00"), L("2026-11-09T06:00"))
    after = bv(3, L("2026-11-15T12:00"), L("2026-11-17T12:00"))
    gone = bv(4, L("2026-11-02T10:00"), L("2026-11-09T00:00"))  # ends at the week's start
    bars = {b.booking.id: b for b in layout_bars([inside, before, after, gone], w, now)}
    assert set(bars) == {1, 2, 3}
    a = bars[1]
    assert a.left == pytest.approx(34 / 168 * 100, abs=1e-3)
    assert a.width == pytest.approx(4 / 168 * 100, abs=1e-3)
    assert not a.from_before and not a.into_after and not a.past
    b = bars[2]
    assert b.left == 0 and b.from_before and not b.into_after and b.past
    assert b.width == pytest.approx(6 / 168 * 100, abs=1e-3)
    c = bars[3]
    assert c.into_after and not c.from_before
    assert c.left + c.width == pytest.approx(100, abs=1e-3)


def test_partner_goes_to_lane_one_and_both_name_each_other():
    w = week_containing(date(2026, 11, 9), TZ)
    host = bv(1, L("2026-11-10T09:00"), L("2026-11-10T18:00"), shareable=True, name="Chiara",
              resource="cpu_gpu")
    partner = bv(2, L("2026-11-10T11:00"), L("2026-11-10T15:00"), is_shared=True, name="Dario")
    later = bv(3, L("2026-11-11T09:00"), L("2026-11-11T10:00"), name="Bob")
    bars = {b.booking.id: b for b in layout_bars([host, partner, later], w, w.start)}
    assert (bars[1].lane, bars[2].lane, bars[3].lane) == (0, 1, 0)
    assert bars[1].shared_with == ("Dario",) and bars[2].shared_with == ("Chiara",)
    assert bars[3].shared_with == ()
    assert bars[1].title(TZ) == "Chiara · CPU + GPU · Tue 10 09:00 – Tue 10 18:00 · shared with Dario"
    assert bars[2].title(TZ).endswith("shared with Chiara")


def test_title_says_shareable_when_alone():
    w = week_containing(date(2026, 11, 9), TZ)
    b = bv(1, L("2026-11-10T09:00"), L("2026-11-10T18:00"), shareable=True, name="Bob")
    assert layout_bars([b], w, w.start)[0].title(TZ) == (
        "Bob · CPU · Tue 10 09:00 – Tue 10 18:00 · shareable")


@pytest.mark.parametrize("day, label", [
    (date(2026, 11, 10), "9 – 15 Nov 2026"),
    (date(2026, 10, 1), "28 Sep – 4 Oct 2026"),
    (date(2025, 12, 31), "29 Dec 2025 – 4 Jan 2026"),
])
def test_range_label(day, label):
    assert range_label(week_containing(day, TZ)) == label


def test_short_booking_keeps_a_positive_width():
    w = week_containing(date(2026, 11, 9), TZ)
    b = bv(1, L("2026-11-10T09:00"), L("2026-11-10T09:00") + timedelta(hours=1))
    assert layout_bars([b], w, w.start)[0].width > 0


@pytest.mark.parametrize("hours, label", [
    (30, "Maria Francesca"), (8, "Maria"), (6, "MF"), (4, "M"),
])
def test_label_shortens_with_the_bar(hours, label):
    w = week_containing(date(2026, 11, 9), TZ)
    start = L("2026-11-10T09:00")
    b = bv(1, start, start + timedelta(hours=hours), name="Maria Francesca")
    assert layout_bars([b], w, w.start)[0].label == label


def test_label_single_word_name():
    w = week_containing(date(2026, 11, 9), TZ)
    start = L("2026-11-10T09:00")
    assert layout_bars([bv(1, start, start + timedelta(hours=6), name="Bob")], w,
                       w.start)[0].label == "Bo"
