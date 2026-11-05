import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import clock
from app.crypto import SecretBox, load_or_create_key
from app.db import make_engine, make_sessionmaker
from app.services import setup
from app.settings_store import SettingsStore
from app.web import portal
from app.web.deps import LoginRequired, make_signer

setup_log = logging.getLogger("app.setup")
STATIC_DIR = Path(__file__).parent / "static"


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

    @app.middleware("http")
    async def reject_foreign_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and origin is not None:
            if urlsplit(origin).netloc != request.headers.get("host"):
                return HTMLResponse("Cross-origin request rejected.", status_code=403)
        return await call_next(request)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        resp = RedirectResponse("/login", status_code=303)
        if request.headers.get("hx-request"):
            resp.headers["HX-Redirect"] = "/login"
        return resp

    @app.exception_handler(PermissionError)
    async def _forbidden(request: Request, exc: PermissionError):
        return HTMLResponse("You are not allowed to do that.", status_code=403)

    @app.exception_handler(LookupError)
    async def _not_found(request: Request, exc: LookupError):
        if type(exc) is not LookupError:  # KeyError/IndexError are bugs, not missing rows
            raise exc
        return HTMLResponse("Not found.", status_code=404)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(portal.router)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app
