import asyncio
import itertools
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

import app.models  # noqa: F401  (register tables on Base.metadata)
from app.db import Base, make_sessionmaker
from app.web.main import create_app


@pytest.fixture(scope="session")
def pg_url():
    with PostgresContainer("postgres:16") as pg:
        url = pg.get_connection_url()  # postgresql+psycopg2://...
        yield "postgresql+asyncpg://" + url.split("://", 1)[1]


def _upgrade_head(url: str) -> None:
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture(scope="session")
async def _engine(pg_url):
    # env.py calls asyncio.run, so run alembic in a worker thread.
    await asyncio.to_thread(_upgrade_head, pg_url)
    engine = create_async_engine(pg_url)
    yield engine
    await engine.dispose()


async def _truncate(engine):
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def sessionmaker(_engine):
    await _truncate(_engine)  # clean at setup so a failed test can't leak dirt
    yield make_sessionmaker(_engine)
    await _truncate(_engine)


@pytest_asyncio.fixture
async def db(sessionmaker):
    async with sessionmaker() as session:
        yield session


@pytest_asyncio.fixture
async def client(sessionmaker, tmp_path):
    app = create_app(sessionmaker, data_dir=tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


_counter = itertools.count(1)


@pytest_asyncio.fixture
async def make_user(db):
    async def factory(**kw):
        n = next(_counter)
        kw.setdefault("telegram_id", 1_000_000 + n)
        kw.setdefault("display_name", f"User {n}")
        user = app.models.User(**kw)
        db.add(user)
        await db.flush()
        return user

    return factory


@pytest_asyncio.fixture
async def make_server(db):
    async def factory(**kw):
        n = next(_counter)
        kw.setdefault("name", f"server-{n}")
        kw.setdefault("hostname", f"server-{n}.lab")
        kw.setdefault("kind", "standalone")
        server = app.models.Server(**kw)
        db.add(server)
        await db.flush()
        return server

    return factory
