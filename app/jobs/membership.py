from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.telegram_gateway import TelegramGateway
from app.services import auth
from app.settings_store import K, SettingsStore


async def membership_sync(
    db: AsyncSession, store: SettingsStore, gateway: TelegramGateway, now: datetime
) -> auth.SyncResult | None:
    group_id = await store.get(K.TG_GROUP_ID)
    if group_id is None:
        return None
    return await auth.sync_membership(db, store, gateway, int(group_id), now)
