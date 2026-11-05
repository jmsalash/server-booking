from datetime import datetime, timezone

from app.services.setup import complete_setup, ensure_setup_code, verify_setup_code
from app.settings_store import K

now = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)


async def test_setup_code_only_until_completed(db, store):
    code = await ensure_setup_code(db, store, now, reset=False)
    assert len(code) == 14 and code.count("-") == 2
    assert await verify_setup_code(store, code)
    assert await verify_setup_code(store, " " + code.lower().replace("-", "") + " ")
    assert not await verify_setup_code(store, "AAAA-AAAA-AAAA")
    await complete_setup(store)
    assert await ensure_setup_code(db, store, now, reset=False) is None
    assert not await verify_setup_code(store, code)
    assert await store.get(K.SETUP_COMPLETED) is True


async def test_hash_not_plaintext(db, store):
    code = await ensure_setup_code(db, store, now, reset=False)
    h = await store.get(K.SETUP_CODE_HASH)
    assert code.replace("-", "") not in h and len(h) == 64


async def test_reset_setup_issues_new_code(db, store):
    old = await ensure_setup_code(db, store, now, reset=False)
    await complete_setup(store)
    new = await ensure_setup_code(db, store, now, reset=True)
    assert new and new != old
    assert await verify_setup_code(store, new)
    assert not await verify_setup_code(store, old)
