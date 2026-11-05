from sqlalchemy import text


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


async def test_db_roundtrip(db):
    assert (await db.execute(text("select 1"))).scalar() == 1
