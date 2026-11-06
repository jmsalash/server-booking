from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.types import ServerInfo
from app.models import Server

_FIELDS = frozenset(Server.__table__.columns.keys()) - {"id"}


def _check_fields(fields: dict) -> None:
    unknown = set(fields) - _FIELDS
    if unknown:
        raise TypeError(f"unknown server fields: {sorted(unknown)}")


async def list_servers(db: AsyncSession, include_disabled: bool = True) -> list[Server]:
    """Live servers (deleted/archived ones are never listed here)."""
    q = select(Server).where(Server.archived_at.is_(None)).order_by(Server.name)
    if not include_disabled:
        q = q.where(Server.enabled.is_(True))
    return list((await db.scalars(q)).all())


async def list_archived(db: AsyncSession) -> list[Server]:
    q = select(Server).where(Server.archived_at.is_not(None)).order_by(
        Server.archived_at.desc(), Server.name)
    return list((await db.scalars(q)).all())


async def get_live(db: AsyncSession, server_id: int) -> Server:
    """A server that has not been deleted; LookupError otherwise."""
    server = await db.get(Server, server_id)
    if server is None or server.archived_at is not None:
        raise LookupError(f"server {server_id} not found")
    return server


async def create_server(db: AsyncSession, **fields) -> Server:
    _check_fields(fields)
    server = Server(**fields)
    db.add(server)
    await db.flush()
    return server


async def update_server(db: AsyncSession, server_id: int, **fields) -> Server:
    _check_fields(fields)
    server = await db.get(Server, server_id)
    if server is None:
        raise LookupError(f"server {server_id} not found")
    for key, value in fields.items():
        setattr(server, key, value)
    await db.flush()
    return server


def to_info(server: Server) -> ServerInfo:
    return ServerInfo(
        id=server.id,
        name=server.name,
        kind=server.kind,
        has_gpu=bool(server.has_gpu),
        gpu_count=server.gpu_count or 0,
        enabled=bool(server.enabled),
    )
