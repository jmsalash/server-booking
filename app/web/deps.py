from collections.abc import AsyncIterator
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from app import clock
from app.domain.status import format_day_hm
from app.models import User
from app.services import auth
from app.settings_store import SettingsStore

COOKIE = "sb_session"
SESSION_MAX_AGE = int(auth.SESSION_TTL.total_seconds())
LOGIN_COOKIE = "sb_login"  # binds a login token to the browser that requested it
LOGIN_MAX_AGE = 2 * int(auth.TOKEN_TTL.total_seconds())

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["day_hm"] = format_day_hm
templates.env.filters["local_input"] = (
    lambda dt, tz: dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M")
)


class LoginRequired(Exception):
    """Raised by current_user; main.py turns it into a redirect to /login."""


def make_signer(key: bytes) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(key, salt="sb-session")


def make_login_signer(key: bytes) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(key, salt="sb-login")


def login_token_from_cookie(request: Request) -> str | None:
    raw = request.cookies.get(LOGIN_COOKIE)
    if not raw:
        return None
    try:
        return request.app.state.login_signer.loads(raw, max_age=LOGIN_MAX_AGE)
    except BadSignature:
        return None


def session_id_from_cookie(request: Request) -> str | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        return request.app.state.signer.loads(raw, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


async def get_store(request: Request, db: AsyncSession = Depends(get_db)) -> SettingsStore:
    return SettingsStore(db, request.app.state.box)


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    sid = session_id_from_cookie(request)
    user = await auth.session_user(db, sid, clock.utcnow()) if sid else None
    if user is None:
        raise LoginRequired()
    return user


async def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admins only")
    return user
