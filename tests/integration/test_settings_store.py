import json

from app.domain.rules import Rules
from app.models import Setting
from app.settings_store import K, SettingsStore


async def test_secret_stored_encrypted(db, box):
    st = SettingsStore(db, box)
    await st.set(K.TG_TOKEN, "123:abc", secret=True)
    row = await db.get(Setting, K.TG_TOKEN)
    assert row.is_secret and "123:abc" not in json.dumps(row.value)
    assert await st.get(K.TG_TOKEN) == "123:abc"


async def test_plain_value_roundtrip_and_default(db, store):
    assert await store.get(K.PORTAL_BASE_URL, "dflt") == "dflt"
    await store.set(K.PORTAL_BASE_URL, "https://x")
    await store.set(K.TG_TOPIC_POSTS, {"new_booking": True})
    assert await store.get(K.PORTAL_BASE_URL) == "https://x"
    assert await store.get(K.TG_TOPIC_POSTS) == {"new_booking": True}


async def test_set_overwrites(db, store):
    await store.set(K.TG_GROUP_ID, 1)
    await store.set(K.TG_GROUP_ID, 2)
    assert await store.get(K.TG_GROUP_ID) == 2


async def test_rules_default_when_unset(store):
    assert await store.get_rules() == Rules()


async def test_rules_partial_dict_fills_defaults(store):
    await store.set(K.RULES, {"soon_window_min": 60, "bogus": 1})
    r = await store.get_rules()
    assert r.soon_window_min == 60 and r.max_booking_hours == 336


async def test_rules_roundtrip_work_days_tuple(store):
    r = Rules(work_days=(0, 2, 4), idle_cpu_pct=7.5)
    await store.set_rules(r)
    got = await store.get_rules()
    assert got == r and isinstance(got.work_days, tuple)
