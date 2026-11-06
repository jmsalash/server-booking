from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class Rules:
    timezone: str = "Europe/Rome"
    max_booking_hours: int = 336
    soon_window_min: int = 180
    work_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    work_start: str = "09:00"
    work_end: str = "19:00"
    idle_window_min: int = 120
    idle_gpu_util_pct: float = 5.0
    idle_cpu_pct: float = 10.0
    idle_ram_delta_pp: float = 2.0
    max_idle_warnings: int = 3
    # DM the people using a machine when its RAM or a disk is at least this full.
    ram_alert_pct: float = 90.0
    disk_alert_pct: float = 90.0
    stale_after_min: int = 15
    snapshot_retention_h: int = 48

    @classmethod
    def from_dict(cls, d: dict) -> "Rules":
        names = {f.name for f in fields(cls)}
        # Unknown keys (e.g. the old sharing and high-usage alert settings) are ignored.
        kw = {k: v for k, v in d.items() if k in names}
        if "work_days" in kw:
            kw["work_days"] = tuple(kw["work_days"])
        return cls(**kw)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["work_days"] = list(self.work_days)
        return d
