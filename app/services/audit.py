from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def record(
    db: AsyncSession,
    action: str,
    target: str,
    *,
    now: datetime,
    details: dict | None = None,
    actor_user_id: int | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            target=target,
            details=details or {},
            ts=now,
        )
    )
    await db.flush()
