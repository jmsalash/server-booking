import os
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker


def create_app(
    sessionmaker: async_sessionmaker | None = None,
    data_dir: Path | None = None,
) -> FastAPI:
    if data_dir is None:
        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    app = FastAPI(title="Server Booking")
    app.state.sessionmaker = sessionmaker
    app.state.data_dir = data_dir

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app
