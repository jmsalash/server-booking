import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Protocol

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


class CalendarClient(Protocol):
    async def create_event(
        self, title: str, description: str, start: datetime, end: datetime
    ) -> str: ...

    async def update_event(
        self, event_id: str, title: str, description: str, start: datetime, end: datetime
    ) -> None: ...

    async def delete_event(self, event_id: str) -> None: ...

    async def probe(self) -> None: ...


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _body(title: str, description: str, start: datetime, end: datetime) -> dict:
    return {
        "summary": title,
        "description": description,
        "start": {"dateTime": _rfc3339(start), "timeZone": "UTC"},
        "end": {"dateTime": _rfc3339(end), "timeZone": "UTC"},
    }


class GoogleCalendarClient:
    def __init__(self, service_account_json: str, calendar_id: str):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(
            json.loads(service_account_json), scopes=SCOPES
        )
        self._events = build("calendar", "v3", credentials=creds, cache_discovery=False).events()
        self._cal = calendar_id

    async def create_event(self, title, description, start, end) -> str:
        req = self._events.insert(calendarId=self._cal, body=_body(title, description, start, end))
        return (await asyncio.to_thread(req.execute))["id"]

    async def update_event(self, event_id, title, description, start, end) -> None:
        req = self._events.update(
            calendarId=self._cal, eventId=event_id, body=_body(title, description, start, end)
        )
        await asyncio.to_thread(req.execute)

    async def delete_event(self, event_id) -> None:
        from googleapiclient.errors import HttpError

        req = self._events.delete(calendarId=self._cal, eventId=event_id)
        try:
            await asyncio.to_thread(req.execute)
        except HttpError as e:
            if e.resp.status not in (404, 410):
                raise

    async def probe(self) -> None:
        start = datetime.now(timezone.utc)
        eid = await self.create_event(
            "server-booking connection test", "Safe to delete", start, start + timedelta(minutes=1)
        )
        await self.delete_event(eid)
