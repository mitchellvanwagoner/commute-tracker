"""Configuration loading.

A route can be configured two ways:

1. Environment variables (single route) -- the simplest setup.
2. ``routes.yml`` (one or more routes) -- for tracking several commutes at once.

If ``routes.yml`` exists it wins; otherwise the environment is used.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv

load_dotenv()

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


class ConfigError(ValueError):
    """Raised when the configuration is missing or malformed."""


def parse_time(value: str | time) -> time:
    """Parse ``HH:MM`` (24h) into a :class:`datetime.time`."""
    if isinstance(value, time):
        return value
    match = _TIME_RE.match(str(value).strip())
    if not match:
        raise ConfigError(f"Expected a time like '07:30', got {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"Time out of range: {value!r}")
    return time(hour, minute)


def parse_days(value: str | list[str]) -> list[str]:
    """Parse ``mon,tue,wed`` (or a YAML list) into normalized day names."""
    parts = value if isinstance(value, list) else str(value).split(",")
    days = []
    for part in parts:
        day = str(part).strip().lower()[:3]
        if day not in DAY_NAMES:
            raise ConfigError(f"Unknown day {part!r}; use one of {DAY_NAMES}")
        if day not in days:
            days.append(day)
    if not days:
        raise ConfigError("At least one day must be selected")
    return sorted(days, key=DAY_NAMES.index)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "route"


@dataclass(frozen=True)
class Route:
    """One commute to track."""

    id: str
    name: str
    origin: str
    destination: str
    window_start: time
    window_end: time
    interval_minutes: int
    days: list[str]
    timezone: str
    notify_at: time | None = None
    notify_days: list[str] | None = None

    def __post_init__(self) -> None:
        if self.window_end <= self.window_start:
            raise ConfigError(
                f"[{self.name}] window_end ({self.window_end}) must be after "
                f"window_start ({self.window_start})"
            )
        if self.interval_minutes < 1:
            raise ConfigError(f"[{self.name}] interval_minutes must be >= 1")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"[{self.name}] unknown timezone {self.timezone!r}") from exc

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def digest_days(self) -> list[str]:
        """Days the daily report goes out; the tracked days unless overridden."""
        return self.notify_days or self.days

    def sample_times(self) -> list[time]:
        """Every clock time inside the window, stepping by ``interval_minutes``.

        The window is inclusive of both ends when the interval divides it evenly.
        """
        start = self.window_start.hour * 60 + self.window_start.minute
        end = self.window_end.hour * 60 + self.window_end.minute
        return [
            time(minute // 60, minute % 60)
            for minute in range(start, end + 1, self.interval_minutes)
        ]

    @classmethod
    def from_dict(cls, data: dict, defaults: dict | None = None) -> Route:
        merged = {**(defaults or {}), **data}
        missing = [k for k in ("origin", "destination") if not merged.get(k)]
        if missing:
            raise ConfigError(f"Route is missing required field(s): {', '.join(missing)}")
        name = str(merged.get("name") or "commute").strip()
        return cls(
            id=str(merged.get("id") or _slugify(name)),
            name=name,
            origin=str(merged["origin"]).strip(),
            destination=str(merged["destination"]).strip(),
            window_start=parse_time(merged.get("window_start", "07:00")),
            window_end=parse_time(merged.get("window_end", "09:00")),
            interval_minutes=int(merged.get("interval_minutes", 15)),
            days=parse_days(merged.get("days", "mon,tue,wed,thu,fri")),
            timezone=str(merged.get("timezone", "UTC")),
            notify_at=parse_time(merged["notify_at"]) if merged.get("notify_at") else None,
            notify_days=parse_days(merged["notify_days"]) if merged.get("notify_days") else None,
        )


@dataclass(frozen=True)
class NotifySettings:
    """How the daily report is scored. Delivery lives in :mod:`commute_tracker.notify`."""

    baseline_days: int = 30
    min_baseline_days: int = 5
    threshold: float = 1.0


@dataclass(frozen=True)
class Settings:
    """Everything the app needs to run."""

    api_key: str
    routes: list[Route]
    db_path: Path
    host: str = "0.0.0.0"
    port: int = 8080
    routes_file: Path | None = None
    traffic_model: str = "TRAFFIC_AWARE"
    request_timeout: float = 20.0
    notify: NotifySettings = field(default_factory=NotifySettings)
    extras: dict = field(default_factory=dict)


def _routes_from_env() -> list[Route]:
    return [
        Route.from_dict(
            {
                "name": os.getenv("ROUTE_NAME", "commute"),
                "origin": os.getenv("ORIGIN_ADDRESS", ""),
                "destination": os.getenv("DESTINATION_ADDRESS", ""),
                "window_start": os.getenv("WINDOW_START", "07:00"),
                "window_end": os.getenv("WINDOW_END", "09:00"),
                "interval_minutes": os.getenv("SAMPLE_INTERVAL_MINUTES", "15"),
                "days": os.getenv("DAYS", "mon,tue,wed,thu,fri"),
                # TZ is the conventional Docker variable; TIMEZONE is the
                # explicit fallback, since some shells strip TZ from the
                # environment they hand to child processes.
                "timezone": os.getenv("TIMEZONE") or os.getenv("TZ") or "UTC",
                "notify_at": os.getenv("NOTIFY_AT", "").strip() or None,
                "notify_days": os.getenv("NOTIFY_DAYS", "").strip() or None,
            }
        )
    ]


def _routes_from_file(path: Path) -> list[Route]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or "routes" not in raw:
        raise ConfigError(f"{path} must contain a top-level 'routes:' list")
    defaults = raw.get("defaults") or {}
    routes = [Route.from_dict(entry, defaults) for entry in raw["routes"]]
    if not routes:
        raise ConfigError(f"{path} defines no routes")
    seen: set[str] = set()
    for route in routes:
        if route.id in seen:
            raise ConfigError(f"Duplicate route id {route.id!r} in {path}")
        seen.add(route.id)
    return routes


def load_settings() -> Settings:
    """Build :class:`Settings` from ``routes.yml`` if present, else the environment."""
    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        raise ConfigError(
            "GOOGLE_MAPS_API_KEY is not set. Copy .env.example to .env and add your key."
        )

    routes_file = Path(os.getenv("ROUTES_FILE", "routes.yml"))
    if routes_file.exists():
        routes = _routes_from_file(routes_file)
    else:
        routes_file = None
        routes = _routes_from_env()

    return Settings(
        api_key=api_key,
        routes=routes,
        db_path=Path(os.getenv("DB_PATH", "data/commutes.db")),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
        routes_file=routes_file,
        traffic_model=os.getenv("TRAFFIC_MODEL", "TRAFFIC_AWARE"),
        request_timeout=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
        notify=NotifySettings(
            baseline_days=int(os.getenv("NOTIFY_BASELINE_DAYS", "30")),
            min_baseline_days=int(os.getenv("NOTIFY_MIN_BASELINE_DAYS", "5")),
            threshold=float(os.getenv("NOTIFY_THRESHOLD_SIGMA", "1.0")),
        ),
    )
