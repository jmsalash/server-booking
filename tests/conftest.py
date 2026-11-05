import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from app.db import Base, make_sessionmaker
from app.web.main import create_app


@pytest.fixture(scope="session")
def pg_url():
    with PostgresContainer("postgres:16") as pg:
        url = pg.get_connection_url()  # postgresql+psycopg2://...
        yield "postgresql+asyncpg://" + url.split("://", 1)[1]


@pytest_asyncio.fixture(scope="session")
async def _engine(pg_url):

    engine = create_async_engine(pg_url)
    # Until Task 2 adds alembic migrations, build the schema directly.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def sessionmaker(_engine):
    sm = make_sessionmaker(_engine)
    yield sm
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    if tables:
        async with _engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


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
