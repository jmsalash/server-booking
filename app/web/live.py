"""Live updates for open pages (board, calendar).

Every change that matters sends a Postgres NOTIFY on commit (app/db.py), from the web app
and from the worker alike. This process keeps one connection LISTENing and passes each
notification to the open pages over server-sent events (GET /live); the page then reloads
the part that shows bookings. The pages still refresh every minute on their own, for changes
that come with time, and in case the stream is down.
"""

import asyncio
import logging
import signal
import threading
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from app import clock
from app.db import LIVE_CHANNEL
from app.services import auth
from app.web.deps import session_id_from_cookie

log = logging.getLogger(__name__)
router = APIRouter()

PING_EVERY = 25.0  # seconds; keeps proxies from closing an idle stream
CHECK_EVERY = 30.0  # seconds between checks that the LISTEN connection is still alive
RETRY_AFTER = 5.0  # seconds before reconnecting a lost LISTEN connection


CHANGED, CLOSING = "changed", "closing"


class LiveHub:
    """The open streams. A page only needs to know "something changed", so each subscriber
    holds at most one pending signal: bursts collapse into one refresh."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[str]] = set()
        self.closing = False

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subs.discard(q)

    def publish(self) -> None:
        for q in list(self._subs):
            if q.empty():
                q.put_nowait(CHANGED)

    def close(self) -> None:
        """End every stream (the server is stopping); the pages reconnect to the new one."""
        self.closing = True
        for q in list(self._subs):
            if not q.empty():
                q.get_nowait()
            q.put_nowait(CLOSING)

    def __len__(self) -> int:
        return len(self._subs)


def close_on_server_exit(hub: LiveHub) -> None:
    """Uvicorn waits for open responses before it stops, and a stream never finishes by
    itself: end the streams as soon as the server is asked to stop, so a restart is quick and
    clean. Uvicorn has no hook for this, so its signal handlers are wrapped (called at
    startup, after uvicorn installed them; it puts back the original ones when it stops)."""
    if threading.current_thread() is not threading.main_thread():
        return  # signal handlers can only be set from the main thread
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(sig)
        if not callable(previous):
            continue

        def handler(signum, frame, previous=previous) -> None:
            loop.call_soon_threadsafe(hub.close)
            previous(signum, frame)

        signal.signal(sig, handler)


async def listen(engine: AsyncEngine, hub: LiveHub) -> None:
    """LISTEN for changes until cancelled, reconnecting if the connection drops."""
    while True:
        try:
            async with engine.connect() as conn:
                raw = (await conn.get_raw_connection()).driver_connection
                await raw.add_listener(LIVE_CHANNEL, lambda *_: hub.publish())
                hub.publish()  # changes may have been missed while disconnected
                while True:
                    await asyncio.sleep(CHECK_EVERY)
                    await raw.execute("SELECT 1")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep listening whatever went wrong
            log.warning("live updates: lost the database connection; retrying", exc_info=True)
            await asyncio.sleep(RETRY_AFTER)


async def _events(hub: LiveHub) -> AsyncIterator[str]:
    q = hub.subscribe()
    try:
        yield "retry: 5000\n\n"
        while not hub.closing:
            try:
                if await asyncio.wait_for(q.get(), PING_EVERY) == CLOSING:
                    return
                yield "data: changed\n\n"
            except TimeoutError:
                yield ": ping\n\n"
    finally:
        hub.unsubscribe(q)


@router.get("/live")
async def live(request: Request):
    # A short session of its own: the stream stays open for long, a DB connection must not.
    sid = session_id_from_cookie(request)
    user = None
    if sid:
        async with request.app.state.sessionmaker() as db:
            user = await auth.session_user(db, sid, clock.utcnow())
    if user is None:
        return Response(status_code=401)  # EventSource gives up; the page keeps polling
    return StreamingResponse(
        _events(request.app.state.live), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
