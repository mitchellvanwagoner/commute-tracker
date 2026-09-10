"""Today's commute, scored against the trailing baseline.

Severity is measured in standard deviations rather than a fixed percentage, so
a route that swings wildly day to day does not cry wolf, while a route that is
normally metronomic flags a smaller absolute slip.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import mean, stdev
from urllib.parse import quote_plus

from .config import Route

MAPS_DIRECTIONS_URL = "https://www.google.com/maps/dir/"

SEVERITY_LABEL = {
    "red": "Busier than normal",
    "yellow": "A typical day",
    "green": "Lighter than normal",
    "grey": "Not enough history yet",
}

# Hex is what a Slack/Discord-style embed wants; the emoji is what a plain push
# notification can actually show in its title.
SEVERITY_HEX = {"red": "#d03b3b", "yellow": "#fab219", "green": "#0ca30c", "grey": "#898781"}
SEVERITY_EMOJI = {"red": "\U0001f534", "yellow": "\U0001f7e1", "green": "\U0001f7e2", "grey": "⚪"}


def directions_url(origin: str, destination: str) -> str:
    """A cross-platform Google Maps link that opens the app on a phone."""
    return (
        f"{MAPS_DIRECTIONS_URL}?api=1"
        f"&origin={quote_plus(origin)}"
        f"&destination={quote_plus(destination)}"
        f"&travelmode=driving"
    )


def _minutes(seconds: float | None) -> float | None:
    return None if seconds is None else seconds / 60


@dataclass(frozen=True)
class Report:
    """Everything a notification (or the dashboard badge) needs."""

    route_id: str
    route_name: str
    local_date: str
    severity: str
    samples: int
    avg_seconds: float | None
    min_seconds: float | None
    max_seconds: float | None
    baseline_avg_seconds: float | None
    baseline_stdev_seconds: float | None
    baseline_days: int
    z_score: float | None
    maps_url: str

    @property
    def label(self) -> str:
        return SEVERITY_LABEL[self.severity]

    @property
    def color(self) -> str:
        return SEVERITY_HEX[self.severity]

    @property
    def emoji(self) -> str:
        return SEVERITY_EMOJI[self.severity]

    @property
    def delta_seconds(self) -> float | None:
        """Today's average minus the baseline average."""
        if self.avg_seconds is None or self.baseline_avg_seconds is None:
            return None
        return self.avg_seconds - self.baseline_avg_seconds

    @property
    def delta_percent(self) -> float | None:
        if self.delta_seconds is None or not self.baseline_avg_seconds:
            return None
        return 100 * self.delta_seconds / self.baseline_avg_seconds

    @property
    def has_data(self) -> bool:
        return self.samples > 0

    def title(self) -> str:
        """One line, short enough to survive a lock screen."""
        if not self.has_data:
            return f"{self.emoji} {self.route_name}: no samples today"
        avg = _minutes(self.avg_seconds)
        delta = self.delta_percent
        if delta is None:
            return f"{self.emoji} {self.route_name}: {avg:.0f} min today"
        direction = "slower" if delta >= 0 else "faster"
        return f"{self.emoji} {self.route_name}: {avg:.0f} min, {abs(delta):.0f}% {direction}"

    def body(self) -> str:
        """The notification body: today's numbers, then what normal looks like."""
        if not self.has_data:
            return (
                f"No commute samples were recorded for {self.local_date}. "
                "Check the dashboard for failed lookups."
            )
        lines = [
            self.label,
            f"Today  avg {_minutes(self.avg_seconds):.1f} min  "
            f"(best {_minutes(self.min_seconds):.1f}, worst {_minutes(self.max_seconds):.1f}) "
            f"from {self.samples} sample{'s' if self.samples != 1 else ''}",
        ]
        if self.baseline_avg_seconds is not None:
            spread = (
                f" ± {_minutes(self.baseline_stdev_seconds):.1f}"
                if self.baseline_stdev_seconds
                else ""
            )
            lines.append(
                f"Normal {_minutes(self.baseline_avg_seconds):.1f} min{spread} "
                f"over the last {self.baseline_days} day"
                f"{'s' if self.baseline_days != 1 else ''}"
            )
            delta = self.delta_seconds
            sign = "+" if delta >= 0 else "−"
            lines.append(f"Today is {sign}{abs(_minutes(delta)):.1f} min vs normal")
        else:
            lines.append("Still building a baseline — a few more days of data needed.")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "route_id": self.route_id,
            "route_name": self.route_name,
            "local_date": self.local_date,
            "severity": self.severity,
            "label": self.label,
            "color": self.color,
            "samples": self.samples,
            "avg_seconds": self.avg_seconds,
            "min_seconds": self.min_seconds,
            "max_seconds": self.max_seconds,
            "baseline_avg_seconds": self.baseline_avg_seconds,
            "baseline_stdev_seconds": self.baseline_stdev_seconds,
            "baseline_days": self.baseline_days,
            "z_score": self.z_score,
            "delta_seconds": self.delta_seconds,
            "delta_percent": self.delta_percent,
            "maps_url": self.maps_url,
            "title": self.title(),
            "body": self.body(),
        }


def classify(z_score: float | None, threshold: float = 1.0) -> str:
    """Map a z-score to a traffic-light severity."""
    if z_score is None:
        return "grey"
    if z_score > threshold:
        return "red"
    if z_score < -threshold:
        return "green"
    return "yellow"


def build_report(
    db,
    route: Route,
    *,
    today: date | None = None,
    baseline_days: int = 30,
    min_baseline_days: int = 5,
    threshold: float = 1.0,
) -> Report:
    """Score ``today`` for ``route`` against the preceding days' daily averages.

    The baseline is built from *daily averages* rather than individual samples,
    so a day with more samples than usual does not dominate it.
    """
    today = today or datetime.now(route.tzinfo).date()
    today_iso = today.isoformat()

    # Explicit date bounds rather than a "last N days" filter: the report must
    # describe the day it is asked about, not the day the query happens to run.
    rows = db.daily_stats(
        route.id,
        since=(today - timedelta(days=baseline_days)).isoformat(),
        until=today_iso,
    )
    today_row = next((row for row in rows if row["local_date"] == today_iso), None)
    prior = [row for row in rows if row["local_date"] < today_iso]

    baseline_avg = baseline_sd = z_score = None
    if len(prior) >= min_baseline_days:
        averages = [row["avg_seconds"] for row in prior]
        baseline_avg = mean(averages)
        baseline_sd = stdev(averages) if len(averages) > 1 else 0.0
        if today_row and baseline_sd:
            z_score = (today_row["avg_seconds"] - baseline_avg) / baseline_sd
        elif today_row:
            # A perfectly steady baseline: any movement at all is the signal.
            difference = today_row["avg_seconds"] - baseline_avg
            z_score = 0.0 if difference == 0 else (threshold + 1) * (1 if difference > 0 else -1)

    return Report(
        route_id=route.id,
        route_name=route.name,
        local_date=today_iso,
        severity=classify(z_score, threshold) if today_row else "grey",
        samples=today_row["samples"] if today_row else 0,
        avg_seconds=today_row["avg_seconds"] if today_row else None,
        min_seconds=today_row["min_seconds"] if today_row else None,
        max_seconds=today_row["max_seconds"] if today_row else None,
        baseline_avg_seconds=baseline_avg,
        baseline_stdev_seconds=baseline_sd,
        baseline_days=len(prior),
        z_score=z_score,
        maps_url=directions_url(route.origin, route.destination),
    )
