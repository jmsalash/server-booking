from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True)
class GpuUsage:
    util_pct: float
    mem_pct: float


@dataclass(frozen=True)
class ServerUsage:
    ts: datetime
    cpu_pct: float
    ram_pct: float
    gpus: tuple[GpuUsage, ...]


@dataclass(frozen=True)
class ServerInfo:
    id: int
    name: str
    kind: str
    has_gpu: bool
    gpu_count: int
    shareable: bool
    enabled: bool


@dataclass(frozen=True)
class BookingView:
    id: int
    user_id: int
    user_name: str
    server_id: int
    start: datetime
    end: datetime
    resource: str
    is_shared: bool


class StatusCode(StrEnum):
    UNAVAILABLE = "unavailable"
    SHAREABLE = "shareable"
    FREE_SOON = "free_soon"
    BOOKED = "booked"
    BOOKED_SOON = "booked_soon"
    AVAILABLE = "available"


@dataclass(frozen=True)
class ServerStatus:
    code: StatusCode
    label: str
    busy_until: datetime | None
    next_busy: datetime | None
    holders: tuple[str, ...]
