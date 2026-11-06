"""Booking from a private chat with the bot: the /book wizard and /mybookings.

Stateless: every step's choices travel in the callback data, and the final step goes through
the same booking service as the web portal, which re-validates everything. Callers resolve the
member (`user`) and own the session.

Callback data (all well under Telegram's 64-byte limit):
  bk                              machine list
  bk:<sid>                        how long
  bk:<sid>:<dur>                  CPU or CPU + GPU (skipped on machines without GPU)
  bk:<sid>:<dur>:<res>            allow sharing? / confirm the join
  bk:<sid>:<dur>:<res>:s0|s1|j<id> book (s = shareable no/yes; j = join booking <id>)
  bkx                             cancel the wizard
  <dur> is a number of hours from the current hour, or U<hours since the epoch> for an end.
  mb                              my bookings
  mb:x|e|c|cy:<booking id>        extend +2 h, end now, cancel?, cancel confirmed
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, time
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.reply import Reply
from app.domain.booking_errors import error_text
from app.domain.rules import Rules
from app.domain.status import format_day_hm, format_hm_or_day
from app.domain.timeutil import floor_hour, is_hour_aligned
from app.domain.types import BookingView, ServerInfo, StatusCode
from app.models import Booking, Server, User
from app.services import board, booking
from app.services.calendar import machine_sub
from app.settings_store import K, SettingsStore

BOOK, CANCEL_FLOW, MINE = "bk", "bkx", "mb"
CANCEL_BUTTON = ("✖ Cancel", CANCEL_FLOW)
AGAIN_BUTTONS = [("Start again", BOOK), CANCEL_BUTTON]
MINE_BUTTON = ("My bookings", MINE)

PICK_MACHINE = "Which machine?"
NO_MACHINE = "No machine is free right now. /notify <name> to get a message."
PICK_DURATION = "For how long, from now?"
PICK_RESOURCE = "What will you use?"
PICK_SHARING = "Allow sharing?"
FLOW_CANCELLED = "OK, nothing booked."
NOT_FREE = "{name} is not free any more."
GONE = "That machine is not available any more."
NO_SLOT = "No booking from now fits on {name}."
NOT_YOURS = "That booking isn't yours or has ended."
INACTIVE = "Your account is not active."
NO_BOOKINGS = "You have no current or upcoming bookings. Book one with /book."
EXTEND_BY = timedelta(hours=2)

RESOURCES = {"cpu": "cpu", "gpu": "cpu_gpu"}  # callback code -> booking resource
RESOURCE_LABEL = {"cpu": "CPU", "cpu_gpu": "CPU + GPU"}
FREE_NOW = (StatusCode.AVAILABLE, StatusCode.BOOKED_SOON)
MAX_ID = 2**31 - 1
MAX_HOURS_CODE = 10_000


class BadData(ValueError):
    """Callback data that no button of ours produces."""


@dataclass(frozen=True)
class Offer:
    """A machine bookable from now: free, or shareable by someone else (`host`)."""
    server: ServerInfo
    sub: str  # `RTX 5090`, `CPU only`
    host: BookingView | None
    bookings: list[BookingView]  # its active bookings, by start


def _id(raw: str) -> int:
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_ID:
        raise BadData(raw)
    return int(raw)


def _hm(dt: datetime, rules: Rules, now: datetime) -> str:
    return format_hm_or_day(dt, rules.timezone, now)


def _span(start: datetime, end: datetime, rules: Rules, now: datetime) -> str:
    return f"{_hm(start, rules, now)}–{format_hm_or_day(end, rules.timezone, start)}"


# --- /book ------------------------------------------------------------------------------------


async def offers(
    db: AsyncSession, store: SettingsStore, user: User, now: datetime
) -> list[Offer]:
    out = []
    for card in await board.build_board(db, store, now):
        if not card.server.enabled:
            continue
        host = None
        if card.status.code == StatusCode.SHAREABLE:
            host = card.current[0]
            if host.user_id == user.id:  # can't join yourself
                continue
        elif card.status.code not in FREE_NOW:
            continue
        gpus = [{"model": g.model} for g in card.usage.gpus] if card.usage else None
        out.append(Offer(card.server, machine_sub(card.server, gpus), host, card.upcoming))
    return out


def _offer_label(o: Offer, rules: Rules, now: datetime) -> str:
    if o.host is not None:
        return (f"{o.server.name} · join {o.host.user_name} "
                f"(until {_hm(o.host.end, rules, now)})")
    return f"{o.server.name} · {o.sub}"


def machine_list(found: list[Offer], rules: Rules, now: datetime) -> Reply:
    if not found:
        return Reply(NO_MACHINE)
    buttons = [(_offer_label(o, rules, now), f"{BOOK}:{o.server.id}") for o in found]
    return Reply(PICK_MACHINE, [*buttons, CANCEL_BUTTON])


def _work_end(day, rules: Rules) -> datetime:
    h, m = rules.work_end.split(":")
    return datetime.combine(day, time(int(h), int(m)), tzinfo=ZoneInfo(rules.timezone)) \
        .astimezone(UTC)


def _abs(end: datetime) -> str:
    return f"U{int(end.timestamp()) // 3600}"


def durations(o: Offer, rules: Rules, now: datetime) -> list[tuple[str, str]]:
    """(label, code) choices for a booking from the current hour on this machine.

    Ends that run into the next booking or past the maximum length are dropped; when the next
    booking (or, joining, the main booking's end) cut a choice, ending exactly then is offered.
    """
    tz = rules.timezone
    start = floor_hour(now, tz)
    host = o.host
    limits = [b.start for b in o.bookings if b.start > now and (host is None or b.id != host.id)]
    if host is not None:
        limits.append(host.end)
    limit = min(limits, default=None)
    longest = start + timedelta(hours=rules.max_booking_hours)
    today = now.astimezone(ZoneInfo(tz)).date()
    candidates = [(f"2 h (until {_hm(_at_least(2, now, tz), rules, now)})", _at_least(2, now, tz), "2"),
                  (f"4 h (until {_hm(_at_least(4, now, tz), rules, now)})", _at_least(4, now, tz), "4")]
    end_today = _work_end(today, rules)
    if end_today > start + timedelta(hours=1):
        candidates.append((f"Until {rules.work_end} today", end_today, _abs(end_today)))
    end_tomorrow = _work_end(today + timedelta(days=1), rules)
    candidates.append((f"Until tomorrow {rules.work_end}", end_tomorrow, _abs(end_tomorrow)))

    chosen: list[tuple[datetime, str, str]] = []
    cut = host is not None
    for label, end, code in candidates:
        if not is_hour_aligned(end, tz) or end > longest or end <= now:
            continue
        if limit is not None and end > limit:
            cut = True
            continue
        if all(end != e for e, _, _ in chosen):
            chosen.append((end, label, code))
    if (cut and limit is not None and limit <= longest and limit > now
            and all(limit != e for e, _, _ in chosen)):
        chosen.append((limit, f"Until {_hm(limit, rules, now)}", _abs(limit)))
    return [(label, code) for _, label, code in sorted(chosen, key=lambda c: c[0])]


def _at_least(hours: int, now: datetime, tz: str) -> datetime:
    """The first full hour at least `hours` from now: at 11:50, "2 h" ends at 14:00."""
    target = now + timedelta(hours=hours)
    end = floor_hour(target, tz)
    return end if end == target else end + timedelta(hours=1)


def _end(code: str, now: datetime, tz: str) -> datetime:
    if code.isdigit() and 1 <= int(code) <= MAX_HOURS_CODE:
        return _at_least(int(code), now, tz)
    if code.startswith("U") and code[1:].isdigit() and len(code) <= 9:
        return datetime.fromtimestamp(int(code[1:]) * 3600, UTC)
    raise BadData(code)


async def _offer(
    db: AsyncSession, store: SettingsStore, user: User, sid: int, now: datetime
) -> Offer | Reply:
    found = next((o for o in await offers(db, store, user, now) if o.server.id == sid), None)
    if found is not None:
        return found
    server = await db.get(Server, sid)
    if server is None or server.archived_at is not None or not server.enabled:
        return Reply(GONE, AGAIN_BUTTONS)
    return Reply(NOT_FREE.format(name=server.name), AGAIN_BUTTONS)


def _share_question(o: Offer, prefix: str, start: datetime, end: datetime, resource: str,
                    rules: Rules, now: datetime) -> Reply:
    text = (f"{PICK_SHARING}\n{o.server.name}, {_span(start, end, rules, now)}, "
            f"{RESOURCE_LABEL[resource]}. Sharing lets one more person join you.")
    return Reply(text, [("Yes", f"{prefix}:s1"), ("No", f"{prefix}:s0"), CANCEL_BUTTON])


def _join_question(name: str, host: BookingView, prefix: str, end: datetime, rules: Rules,
                   now: datetime) -> Reply:
    text = f"You'll share {name} with {host.user_name} until {_hm(end, rules, now)} — confirm?"
    return Reply(text, [("Confirm", f"{prefix}:j{host.id}"), CANCEL_BUTTON])


async def book_step(
    db: AsyncSession, store: SettingsStore, user: User, parts: list[str], now: datetime
) -> Reply:
    """One /book step; `parts` is the callback data after `bk`, split on `:`."""
    rules = await store.get_rules()
    if not parts:
        return machine_list(await offers(db, store, user, now), rules, now)
    if len(parts) > 4:
        raise BadData(parts)
    sid = _id(parts[0])
    start = floor_hour(now, rules.timezone)
    end = _end(parts[1], now, rules.timezone) if len(parts) > 1 else None
    if len(parts) > 2 and parts[2] not in RESOURCES:
        raise BadData(parts[2])
    resource = RESOURCES[parts[2]] if len(parts) > 2 else None
    if len(parts) == 4:
        return await _book(db, store, user, sid, start, end, resource, parts[3], rules, now)

    o = await _offer(db, store, user, sid, now)
    if isinstance(o, Reply):
        return o
    prefix = ":".join([BOOK, *parts])
    if end is None:
        choices = durations(o, rules, now)
        if not choices:
            return Reply(NO_SLOT.format(name=o.server.name), AGAIN_BUTTONS)
        text = f"{PICK_DURATION}\n{o.server.name}, starting {_hm(start, rules, now)}."
        return Reply(text, [*((label, f"{prefix}:{code}") for label, code in choices),
                            CANCEL_BUTTON])
    if resource is None and o.server.has_gpu:
        text = f"{PICK_RESOURCE}\n{o.server.name}, {_span(start, end, rules, now)}."
        return Reply(text, [("CPU", f"{prefix}:cpu"), ("CPU + GPU", f"{prefix}:gpu"),
                            CANCEL_BUTTON])
    if resource is None:
        resource, prefix = "cpu", f"{prefix}:cpu"
    if o.host is not None:  # a partner can't open the booking to a third person
        return _join_question(o.server.name, o.host, prefix, end, rules, now)
    return _share_question(o, prefix, start, end, resource, rules, now)


async def _book(
    db: AsyncSession, store: SettingsStore, user: User, sid: int, start: datetime,
    end: datetime, resource: str, choice: str, rules: Rules, now: datetime,
) -> Reply:
    if choice in ("s0", "s1"):
        join_id = None
    elif choice.startswith("j"):
        join_id = _id(choice[1:])
    else:
        raise BadData(choice)
    server = await db.get(Server, sid)
    if server is None or server.archived_at is not None:
        return Reply(GONE, AGAIN_BUTTONS)
    name = server.name
    prefix = f"{BOOK}:{sid}:{_abs(end)}:{'gpu' if resource == 'cpu_gpu' else 'cpu'}"
    try:
        try:
            b = await booking.create_booking(
                db, store, user, sid, start, end, resource, now, shareable=choice == "s1",
            )
        except booking.ShareConfirmationRequired as e:
            hosts = {x.id for x in e.share_with}
            if join_id is None or hosts != {join_id}:
                # The machine changed since the question was asked: ask about the new partner.
                return _join_question(name, e.share_with[0], prefix, end, rules, now)
            b = await booking.create_booking(
                db, store, user, sid, start, end, resource, now, confirm_shared=True,
            )
    except booking.BookingRejected as e:
        return Reply(f"Couldn't book {name}: {error_text(e.error, rules, e.host, now)}",
                     AGAIN_BUTTONS)
    except LookupError:
        return Reply(GONE, AGAIN_BUTTONS)
    except PermissionError:
        return Reply(INACTIVE)
    text = (f"✅ {name} booked {_span(b.start_at, b.end_at, rules, now)} "
            f"({RESOURCE_LABEL[b.resource]}).")
    if b.is_shared:
        partners = [x for x in await board.active_bookings(db, sid, now)
                    if x.id != b.id and x.start < b.end_at and b.start_at < x.end]
        if partners:
            text += f" Shared with {', '.join(p.user_name for p in partners)}."
    if await store.get(K.GCAL_CALENDAR_ID):
        text += " It's in the calendar."
    return Reply(text, [MINE_BUTTON])


# --- /mybookings ------------------------------------------------------------------------------


async def my_bookings(
    db: AsyncSession, store: SettingsStore, user: User, now: datetime, notice: str | None = None
) -> Reply:
    rules = await store.get_rules()
    tz = rules.timezone
    mine = await booking.user_bookings(db, user.id, now)
    lines = [notice, ""] if notice else []
    if not mine:
        return Reply("\n".join([*lines, NO_BOOKINGS]))
    shared = await booking.partners(db, mine, now)
    lines.append("Your bookings:")
    buttons = []
    for b in mine:
        name = (await db.get(Server, b.server_id)).name
        if b.start <= now:
            line = f"▶️ {name} · now until {_hm(b.end, rules, now)}"
            buttons += [(f"{name}: Extend +2 h", f"{MINE}:x:{b.id}"),
                        (f"{name}: End now", f"{MINE}:e:{b.id}")]
        else:
            line = (f"🗓 {name} · {format_day_hm(b.start, tz)}–"
                    f"{format_hm_or_day(b.end, tz, b.start)}")
            buttons.append((f"{name}: Cancel", f"{MINE}:c:{b.id}"))
        line += f" · {RESOURCE_LABEL.get(b.resource, b.resource)}"
        if shared[b.id]:
            line += f"\n    shared with {', '.join(p.user_name for p in shared[b.id])}"
        lines.append(line)
    return Reply("\n".join(lines), buttons)


async def mine_step(
    db: AsyncSession, store: SettingsStore, user: User, parts: list[str], now: datetime
) -> Reply:
    """A /mybookings button; `parts` is the callback data after `mb`, split on `:`."""
    if not parts:
        return await my_bookings(db, store, user, now)
    if len(parts) != 2 or parts[0] not in ("x", "e", "c", "cy"):
        raise BadData(parts)
    action, bid = parts[0], _id(parts[1])
    b = await db.get(Booking, bid)
    if b is None or b.user_id != user.id or b.status != "active" or b.end_at <= now:
        return Reply(NOT_YOURS, [MINE_BUTTON])
    rules = await store.get_rules()
    name = (await db.get(Server, b.server_id)).name

    if action == "c":
        span = (f"{format_day_hm(b.start_at, rules.timezone)}–"
                f"{format_hm_or_day(b.end_at, rules.timezone, b.start_at)}")
        return Reply(f"Cancel {name} {span}?",
                     [("Yes, cancel", f"{MINE}:cy:{b.id}"), ("No", MINE)])
    try:
        if action == "x":
            new_end = b.end_at + EXTEND_BY
            await booking.extend(db, store, b.id, user, new_end, now)
            notice = f"✅ {name} extended to {_hm(new_end, rules, now)}."
        else:
            if action == "e":
                await booking.end_early(db, store, b.id, user, now)
            else:
                await booking.cancel_booking(db, store, b.id, user, now)
            # The services lock and reload the row, so `b` shows the outcome.
            if b.status == "cancelled":
                notice = f"✅ {name} booking cancelled."
            else:
                notice = f"✅ {name} ends at {_hm(b.end_at, rules, now)}."
    except booking.BookingRejected as e:
        notice = f"Couldn't extend {name}: {error_text(e.error, rules, e.host, now)}"
    except PermissionError:
        return Reply(INACTIVE)
    return await my_bookings(db, store, user, now, notice)
