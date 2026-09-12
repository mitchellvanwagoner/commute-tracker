"""Process configuration and the :class:`Route` type.

The environment holds the things that are true of the whole installation -- the
API key, where to store data, how the daily report is scored. The routes
themselves live in ``routes.yml`` and are managed by :mod:`commute_tracker.routes`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .usage import BILLING_TIMEZONE, FREE_TIER_CALLS

load_dotenv()

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


class ConfigError(ValueError):
    """Raised when the configuration is missing or malformed."""


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read an integer setting, naming the variable when it is unreadable.

    Bare ``int(os.getenv(...))`` at module scope raises a ValueError with no
    hint of which variable is at fault, during import, where nothing can catch
    it -- the container just dies on a traceback.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, int(raw.strip()))
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    """Read a decimal setting. The float counterpart of :func:`_env_int`."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, float(raw.strip()))
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


# Cost guards. Every sample is a billed Routes API call, so a slip in the route
# editor -- a 1 where 15 was meant -- multiplies the monthly bill fifteenfold
# with nothing to show for it: traffic does not move fast enough for minute-by-
# minute sampling to say anything the 5-minute one does not. The limits are
# deliberately generous, and both can be raised from the environment: they exist
# to catch a mistake, not to overrule a deliberate choice.
#
# They gate what may be *created* (see Route.cost_guard_error), never what may
# be loaded, so tightening one cannot strand an install on a routes.yml it can
# no longer parse.
MIN_INTERVAL_MINUTES = _env_int("MIN_INTERVAL_MINUTES", 5)
MAX_SAMPLES_PER_DAY = _env_int("MAX_SAMPLES_PER_DAY", 120)


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


def slugify(name: str) -> str:
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
    enabled: bool = True

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

    def calls_per_month(self) -> int:
        """Billed Routes API calls a full month of this route's schedule would make."""
        return len(self.sample_times()) * len(self.days) * 52 // 12

    def cost_guard_error(self) -> str | None:
        """Why this schedule is too expensive to accept, or None if it is fine.

        Returned rather than raised, and checked on the *write* path only. These
        are a policy about what may be created, not a statement about whether a
        Route is coherent: enforcing them in ``__post_init__`` would also run
        them on load, so an existing ``routes.yml`` written before the limits
        existed would fail to parse and take the whole install down with it --
        including the dashboard that is the only place to fix the offending
        route.
        """
        if self.interval_minutes < MIN_INTERVAL_MINUTES:
            return (
                f"[{self.name}] interval_minutes must be >= {MIN_INTERVAL_MINUTES}; "
                f"got {self.interval_minutes}. Every sample is a paid Routes API call, "
                f"and traffic does not change fast enough for a shorter gap to tell you "
                f"anything new. Raise MIN_INTERVAL_MINUTES if you really mean it."
            )
        samples = len(self.sample_times())
        if samples > MAX_SAMPLES_PER_DAY:
            return (
                f"[{self.name}] this window would take {samples} samples a day, over the "
                f"{MAX_SAMPLES_PER_DAY}/day limit -- roughly "
                f"{self.calls_per_month()} paid Routes API calls a month. "
                f"Widen interval_minutes, shorten the window, or raise MAX_SAMPLES_PER_DAY."
            )
        return None

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
            id=str(merged.get("id") or slugify(name)),
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
            enabled=bool(merged.get("enabled", True)),
        )

    @classmethod
    def from_row(cls, row: dict) -> Route:
        """Rebuild a route from its database row."""
        return cls.from_dict(
            {
                "id": row["id"],
                "name": row["name"],
                "origin": row["origin"],
                "destination": row["destination"],
                "window_start": row["window_start"],
                "window_end": row["window_end"],
                "interval_minutes": row["interval_minutes"],
                "days": row["days"],
                "timezone": row["timezone"],
                "notify_at": row["notify_at"],
                "notify_days": row["notify_days"],
                "enabled": bool(row["enabled"]),
            }
        )

    def to_row(self) -> dict:
        """Flatten to the column shape the routes table stores."""
        return {
            "id": self.id,
            "name": self.name,
            "origin": self.origin,
            "destination": self.destination,
            "window_start": self.window_start.strftime("%H:%M"),
            "window_end": self.window_end.strftime("%H:%M"),
            "interval_minutes": self.interval_minutes,
            "days": ",".join(self.days),
            "timezone": self.timezone,
            "notify_at": self.notify_at.strftime("%H:%M") if self.notify_at else None,
            "notify_days": ",".join(self.notify_days) if self.notify_days else None,
            "enabled": int(self.enabled),
        }

    def as_dict(self) -> dict:
        """JSON shape for the API and the dashboard's route editor."""
        row = self.to_row()
        row["days"] = self.days
        row["notify_days"] = self.notify_days
        row["enabled"] = self.enabled
        row["samples_per_day"] = len(self.sample_times())
        return row


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
    db_path: Path
    routes_file: Path
    host: str = "0.0.0.0"
    port: int = 8080
    traffic_model: str = "TRAFFIC_AWARE"
    request_timeout: float = 20.0
    # The monthly Routes API call ceiling. Defaults to Google's free allowance
    # for the Compute Routes Pro SKU; 0 means no ceiling at all.
    free_tier_calls: int = FREE_TIER_CALLS
    billing_timezone: str = BILLING_TIMEZONE
    # Address suggestions in the route editor: "photon" (free, no key),
    # "google" (Places API, billed), or "off".
    address_provider: str = "photon"
    notify: NotifySettings = field(default_factory=NotifySettings)
    extras: dict = field(default_factory=dict)


def load_settings() -> Settings:
    """Build :class:`Settings` from the environment."""
    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        raise ConfigError(
            "GOOGLE_MAPS_API_KEY is not set. Copy .env.example to .env and add your key."
        )

    db_path = Path(os.getenv("DB_PATH", "data/commutes.db"))
    # Routes sit beside the database by default, so whatever keeps one keeps
    # the other -- one volume to back up, one to carry across a rebuild.
    routes_file = Path(os.getenv("ROUTES_FILE", "") or db_path.parent / "routes.yml")

    return Settings(
        api_key=api_key,
        db_path=db_path,
        routes_file=routes_file,
        host=os.getenv("HOST", "0.0.0.0"),
        # Every number here goes through the _env* helpers rather than a bare
        # int()/float(). Those raise a plain ValueError, which is not a
        # ConfigError, so nothing catches it and a single typo in .env -- a
        # trailing comment, a stray blank, a decimal comma -- takes the
        # container down on a traceback that never names the variable at fault.
        port=_env_int("PORT", 8080),
        traffic_model=os.getenv("TRAFFIC_MODEL", "TRAFFIC_AWARE"),
        request_timeout=_env_float("REQUEST_TIMEOUT_SECONDS", 20.0, minimum=0.1),
        free_tier_calls=_env_int("FREE_TIER_CALLS_PER_MONTH", FREE_TIER_CALLS, minimum=0),
        billing_timezone=os.getenv("BILLING_TIMEZONE", BILLING_TIMEZONE),
        address_provider=os.getenv("ADDRESS_PROVIDER", "photon"),
        notify=NotifySettings(
            baseline_days=_env_int("NOTIFY_BASELINE_DAYS", 30),
            min_baseline_days=_env_int("NOTIFY_MIN_BASELINE_DAYS", 5),
            threshold=_env_float("NOTIFY_THRESHOLD_SIGMA", 1.0),
        ),
    )
