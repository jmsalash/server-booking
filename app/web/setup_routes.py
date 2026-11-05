from datetime import datetime, timedelta

import telegram
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from markupsafe import Markup, escape
from sqlalchemy.ext.asyncio import AsyncSession

from app import clock
from app.integrations.telegram_gateway import BotIdentity, PtbGateway, TelegramGateway
from app.models import User
from app.services import auth, setup
from app.settings_store import K, SettingsStore
from app.web.admin import (
    SECRET_PLACEHOLDER,
    SetupRequired,
    calendar_ctx,
    describe_error,
    grafana_ctx,
    handle_calendar_post,
    handle_grafana_post,
    record,
    redirect,
    render,
)
from app.web.deps import LoginRequired, get_db, get_store, session_id_from_cookie

router = APIRouter(prefix="/admin/setup")

SETUP_COOKIE = "sb_setup"
SETUP_MAX_AGE = 3600
MAX_WRONG_CODES = 10
WRONG_CODE_WINDOW = timedelta(minutes=10)
BIND_WINDOW = timedelta(minutes=15)


def make_setup_signer(key: bytes) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(key, salt="sb-setup")


class _ProbeGateway(PtbGateway):
    """PtbGateway whose get_me initialises and shuts down the bot around the call."""

    async def get_me(self) -> BotIdentity:
        async with self.bot:
            return await super().get_me()


def default_telegram_factory(token: str) -> TelegramGateway:
    return _ProbeGateway(telegram.Bot(token))


# --- access ---------------------------------------------------------------------------------


async def _optional_user(request: Request, db: AsyncSession) -> User | None:
    sid = session_id_from_cookie(request)
    return await auth.session_user(db, sid, clock.utcnow()) if sid else None


def _marker(code_hash: str) -> str:
    # Bound to the current code, so a RESET_SETUP invalidates earlier cookies.
    return code_hash[:16]


async def _has_setup_cookie(request: Request, store: SettingsStore) -> bool:
    raw = request.cookies.get(SETUP_COOKIE)
    code_hash = await store.get(K.SETUP_CODE_HASH)
    if not raw or not code_hash:
        return False
    try:
        value = request.app.state.setup_signer.loads(raw, max_age=SETUP_MAX_AGE)
    except BadSignature:
        return False
    return value == _marker(code_hash)


async def _deny_after_completion(user: User | None) -> None:
    if user is None:
        raise LoginRequired()
    raise PermissionError("the setup wizard is for admins once setup is complete")


async def wizard_user(
    request: Request, db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store)
) -> User | None:
    user = await _optional_user(request, db)
    if user is not None and user.is_admin:
        return user
    if await store.get(K.SETUP_COMPLETED, False):
        await _deny_after_completion(user)
    if not await _has_setup_cookie(request, store):
        raise SetupRequired()
    return user


# --- code -----------------------------------------------------------------------------------


async def _code_gate(request: Request, db: AsyncSession, store: SettingsStore) -> bool:
    """True when the visitor may skip the code page."""
    user = await _optional_user(request, db)
    if user is not None and user.is_admin:
        return True
    if await store.get(K.SETUP_COMPLETED, False):
        await _deny_after_completion(user)
    return await _has_setup_cookie(request, store)


def _render_code(request: Request, error: str | None = None, status: int = 200) -> HTMLResponse:
    return render(request, "admin/setup_code.html", {"user": None, "error": error}, status)


@router.get("")
async def code_page(
    request: Request, db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store)
):
    if await _code_gate(request, db, store):
        return redirect("/admin/setup/telegram")
    return _render_code(request)


def _recent_attempts(request: Request, ip: str, now: datetime) -> list[datetime]:
    attempts: dict[str, list[datetime]] = request.app.state.setup_attempts
    recent = [t for t in attempts.get(ip, []) if now - t < WRONG_CODE_WINDOW]
    if recent:
        attempts[ip] = recent
    else:
        attempts.pop(ip, None)
    return recent


@router.post("")
async def code_submit(
    request: Request, db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store)
):
    if await _code_gate(request, db, store):
        return redirect("/admin/setup/telegram")
    now = clock.utcnow()
    ip = request.client.host if request.client else "unknown"
    recent = _recent_attempts(request, ip, now)
    if len(recent) >= MAX_WRONG_CODES:
        return _render_code(request, "Too many wrong codes. Wait a few minutes and try again.",
                            429)
    form = await request.form()
    if not await setup.verify_setup_code(store, str(form.get("code", ""))):
        request.app.state.setup_attempts[ip] = [*recent, now]
        return _render_code(request, "That code is not valid. Check the server log.", 400)
    request.app.state.setup_attempts.pop(ip, None)
    await record(db, None, "setup_code", "setup", {"ip": ip})
    await db.commit()
    resp = redirect("/admin/setup/telegram")
    resp.set_cookie(
        SETUP_COOKIE,
        request.app.state.setup_signer.dumps(_marker(await store.get(K.SETUP_CODE_HASH))),
        max_age=SETUP_MAX_AGE, path="/admin/setup", httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


# --- telegram -------------------------------------------------------------------------------


async def _render_telegram(
    request: Request, store: SettingsStore, user: User | None,
    error: str | None = None, status: int = 200,
) -> HTMLResponse:
    return render(request, "admin/setup_telegram.html", {
        "user": user, "error": error, "token_set": await store.get(K.TG_TOKEN) is not None,
        "bot_username": await store.get(K.TG_BOT_USERNAME) or "",
        "placeholder": SECRET_PLACEHOLDER,
    }, status)


@router.get("/telegram")
async def telegram_page(
    request: Request, user: User | None = Depends(wizard_user),
    store: SettingsStore = Depends(get_store),
):
    return await _render_telegram(request, store, user)


@router.post("/telegram")
async def telegram_submit(
    request: Request, user: User | None = Depends(wizard_user),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    form = await request.form()
    token = str(form.get("token", "")).strip() or await store.get(K.TG_TOKEN)
    if not token:
        return await _render_telegram(request, store, user, "Enter the bot token from @BotFather.",
                                      400)
    try:
        me = await request.app.state.telegram_factory(token).get_me()
    except Exception as e:  # noqa: BLE001 - shown to the admin
        return await _render_telegram(
            request, store, user, f"Telegram rejected the token — {describe_error(e, [token])}",
            400)
    await store.set(K.TG_TOKEN, token, secret=True)
    await store.set(K.TG_BOT_USERNAME, me.username)
    await store.set(K.PORTAL_BASE_URL, str(request.base_url).rstrip("/"))
    await record(db, user, "setup_telegram", "settings:telegram", {"bot_username": me.username})
    await db.commit()
    return redirect("/admin/setup/bind")


# --- bind -----------------------------------------------------------------------------------

WAITING = Markup(
    '<div id="bind-status" hx-get="/admin/setup/bind/status" hx-trigger="every 2s" '
    'hx-swap="outerHTML"><p class="muted">Waiting for /bind in the group…</p></div>'
)
EXPIRED = Markup(
    '<div id="bind-status"><p>The binding window has expired. '
    '<a href="/admin/setup/bind">Reload this page</a> to reopen it for 15 minutes.</p></div>'
)


async def _status_fragment(store: SettingsStore) -> Markup:
    group = await store.get(K.TG_GROUP_ID)
    topic = await store.get(K.TG_TOPIC_ID)
    if group is not None and topic is not None:
        title = await store.get(K.TG_GROUP_TITLE) or str(group)
        return Markup(
            '<div id="bind-status"><p class="banner">Bound to “{}”</p>'
            '<form method="post" action="/admin/setup/bind/confirm" class="inline">'
            "<button type=\"submit\">That's right — continue</button></form> "
            '<form method="post" action="/admin/setup/bind/reset" class="inline">'
            '<button type="submit">Wrong group — reset</button></form></div>'
        ).format(escape(title))
    until = await store.get(K.SETUP_BIND_OPEN_UNTIL)
    try:
        open_ = bool(until) and clock.utcnow() < datetime.fromisoformat(until)
    except ValueError:
        open_ = False
    return WAITING if open_ else EXPIRED


async def _open_window(store: SettingsStore) -> None:
    await store.set(K.SETUP_BIND_OPEN_UNTIL, (clock.utcnow() + BIND_WINDOW).isoformat())


@router.get("/bind")
async def bind_page(
    request: Request, user: User | None = Depends(wizard_user),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    if await store.get(K.TG_TOKEN) is None:
        return redirect("/admin/setup/telegram")
    await _open_window(store)
    await db.commit()
    return render(request, "admin/setup_bind.html", {
        "user": user, "bot_username": await store.get(K.TG_BOT_USERNAME) or "",
        "status_html": await _status_fragment(store),
    })


@router.get("/bind/status")
async def bind_status(
    user: User | None = Depends(wizard_user), store: SettingsStore = Depends(get_store)
):
    return HTMLResponse(await _status_fragment(store))


@router.post("/bind/confirm")
async def bind_confirm(
    user: User | None = Depends(wizard_user),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    group = await store.get(K.TG_GROUP_ID)
    topic = await store.get(K.TG_TOPIC_ID)
    if group is None or topic is None:
        return redirect("/admin/setup/bind")
    await setup.complete_setup(store)
    await record(db, user, "setup_complete", "setup", {"group_id": group, "topic_id": topic})
    await db.commit()
    return redirect("/admin/setup/grafana")


@router.post("/bind/reset")
async def bind_reset(
    user: User | None = Depends(wizard_user),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    for key in (K.TG_GROUP_ID, K.TG_TOPIC_ID, K.TG_GROUP_TITLE):
        await store.delete(key)
    await _open_window(store)
    await record(db, user, "setup_bind_reset", "settings:telegram")
    await db.commit()
    return redirect("/admin/setup/bind")


# --- grafana / calendar ---------------------------------------------------------------------


async def _render_step(
    request: Request, store: SettingsStore, user: User | None, name: str,
    error: str | None = None, message: str | None = None, status: int = 200,
) -> HTMLResponse:
    ctx = {"user": user, "error": error, "message": message, "placeholder": SECRET_PLACEHOLDER}
    if name == "grafana":
        ctx["grafana"] = await grafana_ctx(store)
    else:
        ctx["calendar"] = await calendar_ctx(store)
    return render(request, f"admin/setup_{name}.html", ctx, status)


@router.get("/grafana")
async def grafana_page(
    request: Request, user: User | None = Depends(wizard_user),
    store: SettingsStore = Depends(get_store),
):
    return await _render_step(request, store, user, "grafana")


@router.post("/grafana")
async def grafana_submit(
    request: Request, user: User | None = Depends(wizard_user),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    if (await request.form()).get("action") == "skip":
        return redirect("/admin/setup/calendar")
    error, message, tested = await handle_grafana_post(request, db, store, user)
    await db.commit()  # an invalid form wrote nothing
    if error or tested:
        return await _render_step(request, store, user, "grafana", error, message,
                                  400 if error else 200)
    return redirect("/admin/setup/calendar")


@router.get("/calendar")
async def calendar_page(
    request: Request, user: User | None = Depends(wizard_user),
    store: SettingsStore = Depends(get_store),
):
    return await _render_step(request, store, user, "calendar")


@router.post("/calendar")
async def calendar_submit(
    request: Request, user: User | None = Depends(wizard_user),
    db: AsyncSession = Depends(get_db), store: SettingsStore = Depends(get_store),
):
    if (await request.form()).get("action") == "skip":
        return redirect("/admin")
    error, message, tested = await handle_calendar_post(request, db, store, user)
    await db.commit()  # an invalid form wrote nothing
    if error or tested:
        return await _render_step(request, store, user, "calendar", error, message,
                                  400 if error else 200)
    return redirect("/admin")
