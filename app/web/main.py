import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import clock
from app.crypto import SecretBox, load_or_create_key
from app.db import make_engine, make_sessionmaker
from app.services import setup
from app.settings_store import SettingsStore
from app.web import admin, portal, setup_routes
from app.web.deps import LoginRequired, make_login_signer, make_signer

setup_log = logging.getLogger("app.setup")
log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
DEADLOCK_SQLSTATE = "40P01"
BUSY_TEXT = "The system was busy — please try again."


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def create_app(
    sessionmaker: async_sessionmaker | None = None,
    data_dir: Path | None = None,
) -> FastAPI:
    for noisy in ("httpx", "httpcore"):  # their INFO logs contain URLs with the bot token
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if data_dir is None:
        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    engine = None
    if sessionmaker is None:
        engine = make_engine(os.environ["DATABASE_URL"])
        sessionmaker = make_sessionmaker(engine)
    key = load_or_create_key(Path(data_dir) / "secret.key")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with sessionmaker() as db:
            store = SettingsStore(db, app.state.box)
            code = await setup.ensure_setup_code(
                db, store, clock.utcnow(), reset=os.environ.get("RESET_SETUP") == "1"
            )
            await db.commit()
        if code:
            setup_log.warning("SETUP CODE: %s", code)
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

    app = FastAPI(title="Server Booking", lifespan=lifespan)
    app.state.sessionmaker = sessionmaker
    app.state.data_dir = data_dir
    app.state.box = SecretBox(key)
    app.state.signer = make_signer(key)
    app.state.login_signer = make_login_signer(key)
    app.state.setup_signer = setup_routes.make_setup_signer(key)
    app.state.setup_attempts = {}  # client ip -> wrong setup-code attempt times
    app.state.telegram_factory = setup_routes.default_telegram_factory
    app.state.grafana_factory = admin.default_grafana_factory
    app.state.calendar_factory = admin.default_calendar_factory

    @app.middleware("http")
    async def reject_foreign_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and origin is not None:
            if urlsplit(origin).netloc != request.headers.get("host"):
                return HTMLResponse("Cross-origin request rejected.", status_code=403)
        return await call_next(request)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.headers.get("hx-request"):
            # A 303 would be followed by the browser and htmx would swap the login page in.
            return Response(status_code=204, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(admin.SetupRequired)
    async def _setup_required(request: Request, exc: admin.SetupRequired):
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": "/admin/setup"})
        return RedirectResponse("/admin/setup", status_code=303)

    @app.exception_handler(PermissionError)
    async def _forbidden(request: Request, exc: PermissionError):
        return HTMLResponse("You are not allowed to do that.", status_code=403)

    @app.exception_handler(DBAPIError)
    async def _db_busy(request: Request, exc: DBAPIError):
        # Concurrent booking writes and the worker jobs lock rows (servers, then bookings by
        # id); if Postgres still detects a deadlock, the victim gets a retry hint, not a 500.
        if _sqlstate(exc) != DEADLOCK_SQLSTATE:
            raise exc
        log.warning("deadlock on %s %s; asked the user to retry",
                          request.method, request.url.path)
        return HTMLResponse(BUSY_TEXT, status_code=409)

    @app.exception_handler(LookupError)
    async def _not_found(request: Request, exc: LookupError):
        if type(exc) is not LookupError:  # KeyError/IndexError are bugs, not missing rows
            raise exc
        return HTMLResponse("Not found.", status_code=404)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(portal.router)
    app.include_router(setup_routes.router)
    app.include_router(admin.router)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app
