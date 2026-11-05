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


class BookingError(StrEnum):
    NOT_HOUR_ALIGNED = "NOT_HOUR_ALIGNED"
    END_NOT_AFTER_START = "END_NOT_AFTER_START"
    IN_PAST = "IN_PAST"
    TOO_LONG = "TOO_LONG"
    NO_GPU = "NO_GPU"
    SERVER_DISABLED = "SERVER_DISABLED"
    CONFLICT = "CONFLICT"
    SHARE_MUST_START_NOW = "SHARE_MUST_START_NOW"
    TOO_MANY_OVERLAPS = "TOO_MANY_OVERLAPS"


@dataclass(frozen=True)
class Validation:
    ok: bool
    error: BookingError | None = None
    is_shared: bool = False
    share_with: str | None = None


def _fail(error: BookingError) -> Validation:
    return Validation(False, error, False, None)


def validate_booking(
    req: BookingRequest,
    existing: Sequence[BookingView],
    server: ServerInfo,
    shareable_now: bool,
    now: datetime,
    rules: Rules,
) -> Validation:
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

    overlaps = [b for b in existing if b.start < req.end and req.start < b.end]
    if not overlaps:
        return Validation(True)
    if not server.shareable or not shareable_now:
        return _fail(BookingError.CONFLICT)
    if len(overlaps) > 1:
        return _fail(BookingError.TOO_MANY_OVERLAPS)
    other = overlaps[0]
    if not (other.start <= now < other.end) or req.start != hour:
        return _fail(BookingError.SHARE_MUST_START_NOW)
    return Validation(True, None, True, other.user_name)
