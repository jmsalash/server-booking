import hashlib
import hmac
import secrets
from datetime import datetime

from app.settings_store import K, SettingsStore

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _normalise(code: str) -> str:
    return code.replace("-", "").strip().upper()


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


async def ensure_setup_code(
    db, store: SettingsStore, now: datetime, reset: bool
) -> str | None:
    if not reset and await store.get(K.SETUP_COMPLETED, False):
        return None
    raw = "".join(secrets.choice(ALPHABET) for _ in range(12))
    await store.set(K.SETUP_CODE_HASH, _hash(raw))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


async def verify_setup_code(store: SettingsStore, code: str) -> bool:
    stored = await store.get(K.SETUP_CODE_HASH)
    if not stored:
        return False
    return hmac.compare_digest(stored, _hash(_normalise(code)))


async def complete_setup(store: SettingsStore) -> None:
    await store.set(K.SETUP_COMPLETED, True)
    await store.set(K.SETUP_CODE_HASH, "")
