from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.domain.rules import Rules
from app.domain.timeutil import floor_hour, is_hour_aligned
from app.domain.types import BookingView, ServerInfo


@dataclass(frozen=True)
class BookingRequest:
    start: datetime
    end: datetime
    resource: str
    user_id: int | None = None  # the booker; overlapping your own booking is a CONFLICT


class BookingError(StrEnum):
    NOT_HOUR_ALIGNED = "NOT_HOUR_ALIGNED"
    END_NOT_AFTER_START = "END_NOT_AFTER_START"
    IN_PAST = "IN_PAST"
    TOO_LONG = "TOO_LONG"
    NO_GPU = "NO_GPU"
    SERVER_DISABLED = "SERVER_DISABLED"
    CONFLICT = "CONFLICT"
    TOO_MANY_OVERLAPS = "TOO_MANY_OVERLAPS"
    SHARE_LOCKED = "SHARE_LOCKED"
    # Joining borrows time inside the shareable booking: it can't start before or end after it.
    OUTSIDE_HOST = "OUTSIDE_HOST"


@dataclass(frozen=True)
class Validation:
    ok: bool
    error: BookingError | None = None
    is_shared: bool = False
    # The shareable booking the request would join (empty unless is_shared).
    share_with: tuple[BookingView, ...] = ()
    # OUTSIDE_HOST: the booking whose window the request must stay inside.
    host: BookingView | None = None


def _fail(error: BookingError) -> Validation:
    return Validation(False, error)


def _overlap(a_start: datetime, a_end: datetime, b: BookingView) -> bool:
    return b.start < a_end and a_start < b.end


def validate_booking(
    req: BookingRequest,
    existing: Sequence[BookingView],
    server: ServerInfo,
    now: datetime,
    rules: Rules,
    paired: frozenset[int] = frozenset(),
    may_join: bool = True,
) -> Validation:
    """Check a new booking (or the new span of an extension) against the server's bookings.

    It may overlap another active booking only by joining it: that booking must be shareable,
    the request must lie entirely inside it (the joiner borrows time; it is not an official
    booking of its own), and at no instant may more than two bookings be active. `paired` are
    bookings the request already shares with (an extension's main booking or partner): they
    count towards the two but are not joined again. With `may_join` False (extensions) any
    other overlap is a CONFLICT.
    """
    tz = rules.timezone
    hour = floor_hour(now, tz)
    if not server.enabled:
        return _fail(BookingError.SERVER_DISABLED)
    if not (is_hour_aligned(req.start, tz) and is_hour_aligned(req.end, tz)):
        return _fail(BookingError.NOT_HOUR_ALIGNED)
    if req.end <= req.start:
        return _fail(BookingError.END_NOT_AFTER_START)
    if req.start < hour:
        return _fail(BookingError.IN_PAST)
    if req.end - req.start > timedelta(hours=rules.max_booking_hours):
        return _fail(BookingError.TOO_LONG)
    if req.resource == "cpu_gpu" and not server.has_gpu:
        return _fail(BookingError.NO_GPU)

    overlaps = [b for b in existing if _overlap(req.start, req.end, b)]
    if not overlaps:
        return Validation(True)
    # Two existing bookings active at the same instant inside the request: a third person.
    for i, x in enumerate(overlaps):
        for y in overlaps[i + 1:]:
            if _overlap(max(req.start, x.start), min(req.end, x.end), y):
                return _fail(BookingError.TOO_MANY_OVERLAPS)
    joined = tuple(b for b in overlaps if b.id not in paired)
    if not joined:  # only the bookings it already shares with
        return Validation(True, None, True)
    if not may_join or any(not b.shareable for b in joined):
        return _fail(BookingError.CONFLICT)
    if req.user_id is not None and any(b.user_id == req.user_id for b in joined):
        return _fail(BookingError.CONFLICT)
    for host in joined:
        if req.start < host.start or req.end > host.end:
            return Validation(False, BookingError.OUTSIDE_HOST, host=host)
    return Validation(True, None, True, joined)
