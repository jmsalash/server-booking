"""User-facing texts for booking errors, shared by the web portal and the Telegram bot."""

from datetime import datetime

from app import clock
from app.domain.booking_rules import BookingError
from app.domain.rules import Rules
from app.domain.status import format_day_hm, format_hm_or_day
from app.domain.types import BookingView

ERROR_TEXT: dict[BookingError, str] = {
    BookingError.SERVER_DISABLED: "This server is not available for booking.",
    BookingError.NOT_HOUR_ALIGNED: "Bookings start and end on the hour.",
    BookingError.END_NOT_AFTER_START: "The end must be after the start.",
    BookingError.IN_PAST: "That booking would already be over — the end must be in the future.",
    BookingError.TOO_FAR_BACK: "A booking can start at most 24 hours in the past.",
    BookingError.TOO_LONG: "Bookings can be at most {limit}.",
    BookingError.NO_GPU: "This server has no GPU — choose CPU only.",
    BookingError.CONFLICT: "That time overlaps another booking.",
    BookingError.TOO_MANY_OVERLAPS: "Two people already share this server at that time.",
    BookingError.SHARE_LOCKED: "Sharing can't be changed while the booking is shared with "
                               "someone.",
    # Normally replaced by outside_host_text, which names the booking and its times.
    BookingError.OUTSIDE_HOST: "You can only join within the other person's booking.",
}


def max_length_text(hours: int) -> str:
    if hours % 24 == 0:
        days = hours // 24
        return f"{days} day" if days == 1 else f"{days} days"
    return f"{hours} hour" if hours == 1 else f"{hours} hours"


def error_text(
    error: BookingError, rules: Rules, host: BookingView | None = None,
    now: datetime | None = None,
) -> str:
    if error == BookingError.OUTSIDE_HOST:
        return outside_host_text(host, rules, now or clock.utcnow())
    return ERROR_TEXT[error].format(limit=max_length_text(rules.max_booking_hours))


def outside_host_text(host: BookingView | None, rules: Rules, now: datetime) -> str:
    """Joining borrows time inside someone's booking; say which edge was crossed."""
    if host is None:
        return "You joined someone else's booking, which has ended — it can't be extended."
    tz = rules.timezone
    if host.start > now:  # not started yet: both edges matter
        return (f"You can only join within {host.user_name}'s booking, "
                f"{format_day_hm(host.start, tz)}–{format_hm_or_day(host.end, tz, host.start)}.")
    return (f"You can only join until {host.user_name}'s booking ends at "
            f"{format_hm_or_day(host.end, tz, now)}.")
