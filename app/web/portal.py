from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import clock
from app.domain.booking_rules import BookingError
from app.domain.idle import IdlePanel, idle_panel
from app.domain.rules import Rules
from app.domain.status import STATUS_LABEL, format_day_hm, format_hm_or_day
from app.domain.timeutil import floor_hour, parse_local
from app.domain.types import BookingView, StatusCode
from app.models import Booking, LoginToken, Server, Session, User
from app.services import auth, board, booking, watchlist
from app.services import usage as usage_svc
from app.services.inventory import get_live, list_servers
from app.settings_store import K, SettingsStore
from app.web.deps import (
    COOKIE,
    LOGIN_COOKIE,
    LOGIN_MAX_AGE,
    SESSION_MAX_AGE,
    current_user,
    get_db,
    get_store,
    login_token_from_cookie,
    session_id_from_cookie,
    templates,
)

router = APIRouter()

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

RESOURCES = ("cpu", "cpu_gpu")
BAD_FORM = "Please choose a server and fill in a valid start and end."
TIMELINE_DAYS = 14
STILL_USING_REFUSED = "That booking isn't yours or has ended."
MAX_ID = 2**31 - 1  # ids are int4; larger values would make asyncpg raise
MIN_YEAR, MAX_YEAR = 2000, 2100
EXPIRED_FRAGMENT = (
    '<p class="notice">Link expired — reload <a href="/login">the login page</a>.</p>'
)


def max_length_text(hours: int) -> str:
    if hours % 24 == 0:
        days = hours // 24
        return f"{days} day" if days == 1 else f"{days} days"
    return f"{hours} hour" if hours == 1 else f"{hours} hours"


def error_text(error: BookingError, rules: Rules, host: BookingView | None = None) -> str:
    if error == BookingError.OUTSIDE_HOST:
        return outside_host_text(host, rules, clock.utcnow())
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


def parse_form_dt(value, tz: str) -> datetime | None:
    """A datetime-local form value as UTC, or None if missing, malformed or out of range."""
    try:
        dt = parse_local(str(value or ""), tz)
    except (ValueError, OverflowError):
        return None
    return dt if MIN_YEAR <= dt.year <= MAX_YEAR else None


def check_id(value: int) -> int:
    """Path ids outside int4 cannot exist: treat them as not found."""
    if not 1 <= value <= MAX_ID:
        raise LookupError(f"id {value} out of range")
    return value


def legend(rules: Rules) -> list[tuple[str, str, str]]:
    soon = f"{rules.soon_window_min / 60:g} h"
    meaning = {
        StatusCode.AVAILABLE: f"Free now, and no booking starts within {soon}.",
        StatusCode.BOOKED_SOON: f"Free now, but a booking starts within {soon}.",
        StatusCode.BOOKED: "In use by a booking.",
        StatusCode.FREE_SOON: f"In use, but free again within {soon}.",
        StatusCode.SHAREABLE: "In use, one more person can join.",
        StatusCode.UNAVAILABLE: "Disabled by an admin; not bookable.",
    }
    order = (StatusCode.AVAILABLE, StatusCode.BOOKED_SOON, StatusCode.BOOKED,
             StatusCode.FREE_SOON, StatusCode.SHAREABLE, StatusCode.UNAVAILABLE)
    return [(c.value, STATUS_LABEL[c], meaning[c]) for c in order]


def _render(request: Request, name: str, ctx: dict, status: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status)


async def _rollback(db: AsyncSession, user: User) -> None:
    """Undo a failed mutation; reload the user, which rollback expires, before re-rendering."""
    await db.rollback()
    await db.refresh(user)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# --- login ----------------------------------------------------------------------------------


@router.get("/login")
async def login_page(
    request: Request, db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store)
):
    bot = await store.get(K.TG_BOT_USERNAME)
    group = await store.get(K.TG_GROUP_ID)
    if not bot or group is None:
        return _render(request, "login.html", {"configured": False})
    token = await auth.create_login_token(db, clock.utcnow())
    await db.commit()
    resp = _render(request, "login.html", {
        "configured": True, "token": token, "link": f"https://t.me/{bot}?start={token}",
    })
    resp.set_cookie(
        LOGIN_COOKIE, request.app.state.login_signer.dumps(token), max_age=LOGIN_MAX_AGE,
        path="/login", httponly=True, samesite="lax", secure=request.url.scheme == "https",
    )
    return resp


@router.get("/login/poll/{token}")
async def login_poll(request: Request, token: str, db: AsyncSession = Depends(get_db)):
    now = clock.utcnow()
    # Only the browser that opened /login for this token may turn it into a session.
    sid = None
    if login_token_from_cookie(request) == token:
        sid = await auth.consume_login(db, token, now)
    if sid is None:
        row = await db.get(LoginToken, token)
        if row is None or row.consumed_at is not None or now >= row.expires_at:
            return HTMLResponse(EXPIRED_FRAGMENT)
        return Response(status_code=204)
    await db.commit()
    resp = Response(status_code=200, headers={"HX-Redirect": "/"})
    resp.set_cookie(
        COOKIE, request.app.state.signer.dumps(sid), max_age=SESSION_MAX_AGE, path="/",
        httponly=True, samesite="lax", secure=request.url.scheme == "https",
    )
    resp.delete_cookie(LOGIN_COOKIE, path="/login", httponly=True, samesite="lax")
    return resp


@router.post("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    sid = session_id_from_cookie(request)
    if sid:
        await db.execute(delete(Session).where(Session.id == sid))
        await db.commit()
    resp = _redirect("/login")
    resp.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax")
    return resp


# --- board ----------------------------------------------------------------------------------


async def _board_ctx(db: AsyncSession, store: SettingsStore, user: User) -> dict:
    now = clock.utcnow()
    cards = await board.build_board(db, store, now)
    rules = await store.get_rules()
    # The viewer's own booking per server (running one first, else the next one), so the
    # card can offer "End my booking" / "Cancel my booking" instead of "Notify me when free".
    mine = {}
    # "Join" links: a single running shareable booking that isn't the viewer's, prefilled with
    # the usual default start (the next hour) up to that booking's end.
    join = {}
    local = templates.env.filters["local_input"]
    for c in cards:
        own = [b for b in c.upcoming if b.user_id == user.id]
        if own:
            b = own[0]
            mine[c.server.id] = {"booking": b, "running": b.start <= now}
        if c.status.code == StatusCode.SHAREABLE and c.current[0].user_id != user.id:
            end = c.current[0].end
            start = floor_hour(now, rules.timezone) + timedelta(hours=1)
            if start >= end:
                start = floor_hour(now, rules.timezone)
            join[c.server.id] = {"with": c.current[0], "start": local(start, rules.timezone),
                                 "end": local(end, rules.timezone)}
    return {"user": user, "cards": cards, "tz": rules.timezone, "legend": legend(rules),
            "status_names": {c.value: label for c, label in STATUS_LABEL.items()},
            "monitoring_down": board.monitoring_down(cards), "mine": mine, "join": join,
            "now": now, "ram_alert": rules.ram_alert_pct, "disk_alert": rules.disk_alert_pct}


@router.get("/")
async def board_page(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    store: SettingsStore = Depends(get_store),
):
    return _render(request, "board.html", await _board_ctx(db, store, user))


@router.get("/board/cards")
async def board_cards(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    store: SettingsStore = Depends(get_store),
):
    return _render(request, "_board_cards.html", await _board_ctx(db, store, user))


# --- booking --------------------------------------------------------------------------------


def _int(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= MAX_ID else None


async def _render_book(
    request: Request, db: AsyncSession, store: SettingsStore, user: User,
    server_id: int | None, values: dict, status: int = 200,
    error: str | None = None, share_with: list[dict] | None = None, warned: dict | None = None,
) -> HTMLResponse:
    now = clock.utcnow()
    rules = await store.get_rules()
    servers = await list_servers(db, include_disabled=False)
    selected = next((s for s in servers if s.id == server_id), servers[0] if servers else None)
    timeline = []
    if selected is not None:
        horizon = now + timedelta(days=TIMELINE_DAYS)
        timeline = [b for b in await board.active_bookings(db, selected.id, now)
                    if b.start < horizon]
    if not values.get("start"):
        start = floor_hour(now, rules.timezone) + timedelta(hours=1)
        local = templates.env.filters["local_input"]
        values = {**values, "start": local(start, rules.timezone),
                  "end": local(start + timedelta(hours=2), rules.timezone)}
    return _render(request, "book.html", {
        "user": user, "servers": servers, "selected": selected, "timeline": timeline,
        "values": values, "error": error, "share_with": share_with, "warned": warned,
        "tz": rules.timezone,
        "legend": legend(rules),
    }, status)


@router.get("/book")
async def book_page(
    request: Request,
    server_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    store: SettingsStore = Depends(get_store),
):
    # Prefilled times (the board's "Join" link); ignored unless both parse.
    tz = (await store.get_rules()).timezone
    values = {}
    if parse_form_dt(start, tz) is not None and parse_form_dt(end, tz) is not None:
        values = {"start": start, "end": end}
    return await _render_book(request, db, store, user, _int(server_id), values)


def _share_confirmed(form, server_id: int, values: dict) -> bool:
    """confirm_shared counts only for the exact server and times the warning was shown for."""
    return (
        form.get("confirm_shared") == "1"
        and form.get("warned_server_id") == str(server_id)
        and form.get("warned_start") == values["start"]
        and form.get("warned_end") == values["end"]
    )


@router.post("/book")
async def book_submit(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    store: SettingsStore = Depends(get_store),
):
    form = await request.form()
    values = {k: str(form.get(k, "")) for k in ("start", "end", "resource")}
    values["shareable"] = form.get("shareable") == "on"
    server_id = _int(form.get("server_id"))
    rules = await store.get_rules()
    start = parse_form_dt(values["start"], rules.timezone)
    end = parse_form_dt(values["end"], rules.timezone)
    server = await db.get(Server, server_id) if server_id is not None else None
    if server is not None and server.archived_at is not None:
        server = None
    if start is None or end is None or server is None or values["resource"] not in RESOURCES:
        return await _render_book(request, db, store, user, server_id, values, 400, BAD_FORM)
    try:
        await booking.create_booking(
            db, store, user, server.id, start, end, values["resource"], clock.utcnow(),
            confirm_shared=_share_confirmed(form, server.id, values),
            shareable=values["shareable"],
        )
    except booking.ShareConfirmationRequired as e:
        await _rollback(db, user)
        warned = {"server_id": str(server_id), "start": values["start"], "end": values["end"]}
        # The times each person is actually shared with: the overlap of the two bookings.
        share_with = [{"name": x.user_name, "resource": x.resource,
                       "start": max(start, x.start), "end": min(end, x.end)}
                      for x in e.share_with]
        return await _render_book(request, db, store, user, server_id, values,
                                  share_with=share_with, warned=warned)
    except booking.BookingRejected as e:
        await _rollback(db, user)
        return await _render_book(request, db, store, user, server_id, values, 400,
                                  error_text(e.error, rules, e.host))
    await db.commit()
    return _redirect("/me")


# --- my bookings ----------------------------------------------------------------------------


async def _render_me(
    request: Request, db: AsyncSession, store: SettingsStore, user: User,
    status: int = 200, error: str | None = None, notice: str | None = None,
) -> HTMLResponse:
    now = clock.utcnow()
    rules = await store.get_rules()
    names = dict((await db.execute(select(Server.id, Server.name))).all())
    bookings = await booking.user_bookings(db, user.id, now)
    return _render(request, "me.html", {
        "user": user, "bookings": bookings,
        "idle": await _idle_panels(db, bookings, now, rules),
        "partners": await booking.partners(db, bookings, now),
        "subs": await watchlist.for_user(db, user.id), "names": names, "now": now,
        "tz": rules.timezone, "error": error, "notice": notice,
    }, status)


async def _idle_panels(
    db: AsyncSession, bookings: list[BookingView], now: datetime, rules: Rules
) -> dict[int, IdlePanel]:
    """Idle panel per running booking on a live, Grafana-monitored machine."""
    out: dict[int, IdlePanel] = {}
    for b in bookings:
        if not b.start <= now < b.end:
            continue
        server = await db.get(Server, b.server_id)
        if server is None or server.kind != "grafana" or server.archived_at is not None:
            continue
        row = await db.get(Booking, b.id)
        snaps = await usage_svc.recent(db, server.id, now - board.QUIET_LOOKBACK)
        out[b.id] = idle_panel(snaps, now, rules, server.has_gpu, b.idle_warnings, row.idle_since,
                               b.start)
    return out


DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _days(days: tuple[int, ...]) -> str:
    """(0, 1, 2, 3, 4) -> "Mon–Fri"; anything else is listed."""
    d = sorted(days)
    if d and d == list(range(d[0], d[-1] + 1)) and len(d) > 2:
        return f"{DAY_NAMES[d[0]]}–{DAY_NAMES[d[-1]]}"
    return ", ".join(DAY_NAMES[i] for i in d)


def _hours(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min" if h and m else (f"{h} h" if h else f"{m} min")


@router.get("/about")
async def about_page(
    request: Request,
    user: User = Depends(current_user),
    store: SettingsStore = Depends(get_store),
):
    rules = await store.get_rules()
    bot = await store.get(K.TG_BOT_USERNAME)
    max_h = rules.max_booking_hours
    return _render(request, "about.html", {
        "user": user, "bot": bot, "legend": legend(rules), "tz": rules.timezone,
        "max_booking": f"{max_h // 24} days" if max_h % 24 == 0 and max_h >= 48 else f"{max_h} h",
        "idle_window": _hours(rules.idle_window_min), "warnings": rules.max_idle_warnings,
        "work": f"{_days(rules.work_days)} {rules.work_start}–{rules.work_end}",
        "idle_cpu": f"{rules.idle_cpu_pct:g}", "idle_gpu": f"{rules.idle_gpu_util_pct:g}",
        "full": _full_text(rules),
    })


def _full_text(rules: Rules) -> str:
    ram, disk = f"{rules.ram_alert_pct:g}%", f"{rules.disk_alert_pct:g}%"
    if ram == disk:
        return f"RAM or a disk on your machine passes {ram}"
    return f"RAM on your machine passes {ram} or a disk passes {disk}"


@router.get("/me")
async def me_page(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    store: SettingsStore = Depends(get_store),
):
    return await _render_me(request, db, store, user)


async def _back(request: Request) -> str:
    """Where to go after a booking action: the board if the form came from there, else /me."""
    return "/" if (await request.form()).get("next") == "/" else "/me"


@router.post("/bookings/{booking_id}/cancel")
async def cancel(
    request: Request,
    booking_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    store: SettingsStore = Depends(get_store),
):
    await booking.cancel_booking(db, store, check_id(booking_id), actor=user, now=clock.utcnow())
    await db.commit()
    return _redirect(await _back(request))


@router.post("/bookings/{booking_id}/end")
async def end_now(
    request: Request,
    booking_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    store: SettingsStore = Depends(get_store),
):
    await booking.end_early(db, store, check_id(booking_id), actor=user, now=clock.utcnow())
    await db.commit()
    return _redirect(await _back(request))


@router.post("/bookings/{booking_id}/still-using")
async def still_using(
    request: Request,
    booking_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    store: SettingsStore = Depends(get_store),
):
    b = await db.get(Booking, check_id(booking_id))
    if b is None:
        raise LookupError(f"booking {booking_id} not found")
    if b.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your booking.")
    now = clock.utcnow()
    if b.status != "active" or b.end_at <= now:
        return await _render_me(request, db, store, user, 400, STILL_USING_REFUSED)
    if not await booking.acknowledge_still_using(db, b.id, user.telegram_id, now):
        await _rollback(db, user)
        return await _render_me(request, db, store, user, 400, STILL_USING_REFUSED)
    await db.commit()
    return await _render_me(request, db, store, user, notice="Idle counter reset.")


@router.post("/bookings/{booking_id}/shareable")
async def set_shareable(
    request: Request,
    booking_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    store: SettingsStore = Depends(get_store),
):
    form = await request.form()
    on = form.get("shareable") == "1"
    rules = await store.get_rules()
    try:
        await booking.set_shareable(db, check_id(booking_id), user, on, clock.utcnow())
    except booking.BookingRejected as e:
        await _rollback(db, user)
        msg = "That booking has ended." if e.error == BookingError.IN_PAST else error_text(
            e.error, rules, e.host)
        return await _render_me(request, db, store, user, 400, msg)
    await db.commit()
    return _redirect("/me")


@router.post("/bookings/{booking_id}/extend")
async def extend(
    request: Request,
    booking_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    store: SettingsStore = Depends(get_store),
):
    form = await request.form()
    rules = await store.get_rules()
    new_end = parse_form_dt(form.get("end"), rules.timezone)
    if new_end is None:
        return await _render_me(request, db, store, user, 400,
                                "Please fill in a valid new end time.")
    try:
        await booking.extend(db, store, check_id(booking_id), user, new_end, clock.utcnow())
    except booking.BookingRejected as e:
        await _rollback(db, user)
        return await _render_me(request, db, store, user, 400, error_text(e.error, rules, e.host))
    await db.commit()
    return _redirect("/me")


# --- watchlist ------------------------------------------------------------------------------


@router.post("/watch/{server_id}")
async def watch(
    server_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    await get_live(db, check_id(server_id))
    await watchlist.subscribe(db, user.id, server_id, clock.utcnow())
    await db.commit()
    return _redirect("/")


@router.post("/watch/{sub_id}/delete")
async def unwatch(
    sub_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    await watchlist.unsubscribe(db, check_id(sub_id), user.id)
    await db.commit()
    return _redirect("/me")
