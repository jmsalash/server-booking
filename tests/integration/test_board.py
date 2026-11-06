from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.types import ServerUsage, StatusCode
from app.models import Booking
from app.services import board, inventory, usage

UTC = timezone.utc
NOW = datetime(2026, 11, 5, 12, 20, tzinfo=UTC)


def at(hour, day=5):
    return datetime(2026, 11, day, hour, 0, tzinfo=UTC)


async def _add(db, user, server, start, end, **kw):
    b = Booking(user_id=user.id, server_id=server.id, start_at=start, end_at=end,
                resource="cpu", **kw)
    db.add(b)
    await db.flush()
    return b


async def _snapshots(db, server, last, pct=20.0):
    for m in range(0, 60, 5):
        await usage.save_snapshot(db, server.id, ServerUsage(last - timedelta(minutes=m), pct, pct, ()))


async def test_board_marks_stale_and_unknown_usage(db, store, make_server):
    server = await make_server(kind="grafana")
    card = await board.build_card(db, store, server.id, NOW)
    assert card.stale is True
    assert card.usage is None
    assert card.status.code == StatusCode.AVAILABLE
    assert card.server == inventory.to_info(server)


async def test_board_standalone_not_stale(db, store, make_server):
    server = await make_server(kind="standalone")
    card = await board.build_card(db, store, server.id, NOW)
    assert (card.stale, card.usage) == (False, None)


async def test_board_fresh_usage(db, store, make_server):
    server = await make_server(kind="grafana")
    await _snapshots(db, server, NOW - timedelta(minutes=2), pct=33.0)
    card = await board.build_card(db, store, server.id, NOW)
    assert card.stale is False
    assert card.usage.ts == NOW - timedelta(minutes=2) and card.usage.cpu_pct == 33.0


async def test_board_shareable_card(db, store, make_server, make_user):
    user = await make_user(display_name="ana")
    server = await make_server(kind="grafana")
    a = await _add(db, user, server, at(11), at(18), shareable=True)
    card = await board.build_card(db, store, server.id, NOW)
    assert card.status.code == StatusCode.SHAREABLE
    assert card.status.holders == ("ana",)
    assert [b.id for b in card.current] == [a.id] and card.current[0].shareable


async def test_board_shared_pair_is_booked_primary_first(db, store, make_server, make_user):
    ana, bob = await make_user(display_name="ana"), await make_user(display_name="bob")
    server = await make_server(kind="grafana")
    b = await _add(db, bob, server, at(11), at(14), is_shared=True)
    a = await _add(db, ana, server, at(12), at(18), shareable=True)
    nxt = await _add(db, bob, server, at(19), at(20))
    card = await board.build_card(db, store, server.id, NOW)
    assert card.status.code == StatusCode.BOOKED
    assert [x.id for x in card.current] == [a.id, b.id]
    assert card.next_start == nxt.start_at


async def test_board_non_shareable_booking_is_booked(db, store, make_server, make_user):
    user = await make_user()
    server = await make_server(kind="grafana")
    await _add(db, user, server, at(11), at(18))
    card = await board.build_card(db, store, server.id, NOW)
    assert card.status.code == StatusCode.BOOKED


async def test_build_board_sorted_disabled_unavailable_and_upcoming(db, store, make_server,
                                                                    make_user):
    user = await make_user(display_name="ana")
    b_srv = await make_server(name="b-srv")
    a_srv = await make_server(name="a-srv", enabled=False)
    await _add(db, user, b_srv, at(8), at(10))  # ended
    later = await _add(db, user, b_srv, at(15), at(17))
    current = await _add(db, user, b_srv, at(11), at(13))
    await _add(db, user, b_srv, at(14), at(15), status="cancelled")
    await _add(db, user, b_srv, at(9, day=20), at(10, day=20))  # beyond 14 days
    cards = await board.build_board(db, store, NOW)
    assert [c.server.name for c in cards] == ["a-srv", "b-srv"]
    assert cards[0].status.code == StatusCode.UNAVAILABLE
    assert [v.id for v in cards[1].upcoming] == [current.id, later.id]
    assert cards[1].upcoming[0].user_name == "ana"
    assert cards[1].status.code == StatusCode.FREE_SOON


async def test_monitoring_down(db, store, make_server):
    assert board.monitoring_down([]) is False
    g1 = await make_server(name="g1", kind="grafana")
    g2 = await make_server(name="g2", kind="grafana")
    await make_server(name="s1", kind="standalone")
    await _snapshots(db, g1, NOW - timedelta(hours=2))
    await _snapshots(db, g2, NOW - timedelta(hours=2))
    cards = await board.build_board(db, store, NOW)
    assert board.monitoring_down(cards) is True
    await _snapshots(db, g1, NOW)
    cards = await board.build_board(db, store, NOW)
    assert board.monitoring_down(cards) is False


async def test_new_servers_wait_for_first_reading(db, store, make_server):
    await make_server(name="g1", kind="grafana")
    await make_server(name="g2", kind="grafana")
    cards = await board.build_board(db, store, NOW)
    assert [c.pending for c in cards] == [True, True]
    assert board.monitoring_down(cards) is False


async def test_never_read_server_failing_in_last_poll_counts_as_down(db, store, make_server):
    await make_server(name="g1", kind="grafana")
    await store.set(board.HEALTH_KEY, {"at": NOW.isoformat(), "ok": 0,
                                       "failed": {"g1": "MetricsUnavailable: no data"}})
    cards = await board.build_board(db, store, NOW)
    assert cards[0].pending is False
    assert board.monitoring_down(cards) is True


async def test_monitoring_down_ignores_disabled(db, store, make_server):
    g1 = await make_server(name="g1", kind="grafana")
    await make_server(name="g2", kind="grafana", enabled=False)
    await _snapshots(db, g1, NOW)
    assert board.monitoring_down(await board.build_board(db, store, NOW)) is False


async def test_inventory_crud(db):
    s = await inventory.create_server(db, name="x", hostname="x.lab", kind="standalone")
    g = await inventory.create_server(db, name="g", hostname="g.lab", kind="grafana",
                                      grafana_instance="g:9100", enabled=False)
    with pytest.raises(TypeError):  # the server-level sharing flag is gone
        await inventory.create_server(db, name="y", hostname="y.lab", kind="grafana",
                                      shareable=True)
    assert [x.name for x in await inventory.list_servers(db)] == ["g", "x"]
    assert [x.name for x in await inventory.list_servers(db, include_disabled=False)] == ["x"]
    g2 = await inventory.update_server(db, g.id, kind="standalone", enabled=True)
    assert g2.kind == "standalone" and g2.enabled is True


async def test_archived_servers_hidden_from_board(db, store, make_server):
    live = await make_server(name="live")
    gone = await make_server(name="gone", archived_at=NOW, enabled=False)
    cards = await board.build_board(db, store, NOW)
    assert [c.server.name for c in cards] == ["live"]
    assert [s.name for s in await inventory.list_servers(db)] == ["live"]
    assert [s.name for s in await inventory.list_archived(db)] == ["gone"]
    with pytest.raises(LookupError):
        await board.build_card(db, store, gone.id, NOW)
    assert (await board.build_card(db, store, live.id, NOW)).server.name == "live"


async def test_card_quiet_since_only_when_in_use_and_quiet_long_enough(db, store, make_server,
                                                                       make_user):
    user = await make_user()
    busy = await make_server(kind="grafana")
    free = await make_server(kind="grafana")
    await _add(db, user, busy, at(9), at(18))
    for server in (busy, free):
        for m in range(0, 150, 5):  # 145 min of quiet
            await usage.save_snapshot(db, server.id, ServerUsage(NOW - timedelta(minutes=m),
                                                                 1.0, 30.0, ()))
    card = await board.build_card(db, store, busy.id, NOW)
    assert card.quiet_since == NOW - timedelta(minutes=145)
    assert (await board.build_card(db, store, free.id, NOW)).quiet_since is None
    await store.set_rules(replace(await store.get_rules(), idle_window_min=180))
    assert (await board.build_card(db, store, busy.id, NOW)).quiet_since is None
