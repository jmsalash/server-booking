import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


_MODEL_NOISE = re.compile(r"^(NVIDIA\s+)?(GeForce\s+)?", re.IGNORECASE)


@dataclass(frozen=True)
class GpuUsage:
    util_pct: float
    mem_pct: float
    index: int | None = None  # device number as nvidia-smi / CUDA_VISIBLE_DEVICES see it
    model: str | None = None  # e.g. "NVIDIA GeForce RTX 5090"

    @property
    def short_model(self) -> str | None:
        """`NVIDIA GeForce RTX 5090` -> `RTX 5090`."""
        return _MODEL_NOISE.sub("", self.model or "").strip() or None

    def label(self, position: int, total: int) -> str:
        """`GPU · RTX 5090` on single-GPU servers, `GPU 1 · RTX 4090` when there are several."""
        name = "GPU"
        if total > 1:
            name += f" {self.index if self.index is not None else position}"
        return f"{name} · {self.short_model}" if self.short_model else name


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
    idle_warnings: int = 0


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
