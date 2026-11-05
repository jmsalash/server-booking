from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import SecretBox
from app.domain.rules import Rules
from app.models import Setting


class K:
    TG_TOKEN = "tg_token"
    TG_BOT_USERNAME = "tg_bot_username"
    TG_GROUP_ID = "tg_group_id"
    TG_TOPIC_ID = "tg_topic_id"
    TG_GROUP_TITLE = "telegram.group_title"
    SETUP_BIND_OPEN_UNTIL = "setup.bind_open_until"  # ISO-8601 UTC; /bind accepted before it
    TG_TOPIC_POSTS = "tg_topic_posts"  # {"new_booking": True, "reclaimed": True, "freed": True}
    GRAFANA_URL = "grafana_url"
    GRAFANA_USER = "grafana_user"
    GRAFANA_PASSWORD = "grafana_password"
    GRAFANA_TOKEN = "grafana_token"
    GRAFANA_DS_UID = "grafana_ds_uid"
    GRAFANA_QUERIES = "grafana_queries"  # keys: cpu, ram, gpu_util, gpu_mem
    GCAL_SA_JSON = "gcal_sa_json"
    GCAL_CALENDAR_ID = "gcal_calendar_id"
    PORTAL_BASE_URL = "portal_base_url"
    SETUP_CODE_HASH = "setup_code_hash"
    SETUP_COMPLETED = "setup_completed"
    RULES = "rules"


SECRET_KEYS = frozenset({K.TG_TOKEN, K.GRAFANA_PASSWORD, K.GRAFANA_TOKEN, K.GCAL_SA_JSON})


class SettingsStore:
    def __init__(self, db: AsyncSession, box: SecretBox):
        self.db = db
        self.box = box

    async def get(self, key: str, default=None):
        row = await self.db.get(Setting, key)
        if row is None:
            return default
        if row.is_secret:
            return self.box.decrypt(row.value["enc"])
        return row.value

    async def set(self, key: str, value, secret: bool = False) -> None:
        # Callers own the transaction: flush only. Never store Python None
        # (JSONB would write a JSON null / SQL NULL ambiguity).
        stored = {"enc": self.box.encrypt(value)} if secret else value
        row = await self.db.get(Setting, key)
        if row is None:
            self.db.add(Setting(key=key, value=stored, is_secret=secret))
        else:
            row.value = stored
            row.is_secret = secret
        await self.db.flush()

    async def delete(self, key: str) -> None:
        row = await self.db.get(Setting, key)
        if row is not None:
            await self.db.delete(row)
            await self.db.flush()

    async def get_rules(self) -> Rules:
        d = await self.get(K.RULES)
        return Rules.from_dict(d) if d else Rules()

    async def set_rules(self, r: Rules) -> None:
        await self.set(K.RULES, r.to_dict())
