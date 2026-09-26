"""Today's commute, scored against the trailing baseline.

Severity is measured in standard deviations rather than a fixed percentage, so
a route that swings wildly day to day does not cry wolf, while a route that is
normally metronomic flags a smaller absolute slip.

A day still in progress is not scored on its average so far. Traffic builds
through a commute window, so the first few samples are the fastest of the day
and their average would call every morning light. Instead a smooth curve is
fitted to what each clock time usually costs, scaled to today's samples so far,
and read off at the times still to come to project the whole day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from statistics import mean, stdev
from urllib.parse import quote_plus

import numpy as np
from numpy.polynomial import Polynomial

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
    # The whole window's average as projected from the samples so far; equal to
    # avg_seconds once every sample time has been measured.
    projected_avg_seconds: float | None = None
    # Today over the usual curve: 1.15 is running 15% heavier than normal.
    curve_scale: float | None = None
    # One point per sample time: {"local_time", "seconds", "measured"}.
    projection: tuple[dict, ...] = field(default=())

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
    def in_progress(self) -> bool:
        """Whether some of today's sample times are still to come."""
        return any(not point["measured"] for point in self.projection)

    @property
    def day_avg_seconds(self) -> float | None:
        """The number today is judged on: the projection if there is one."""
        if self.projected_avg_seconds is not None:
            return self.projected_avg_seconds
        return self.avg_seconds

    @property
    def delta_seconds(self) -> float | None:
        """Today's (projected) average minus the baseline average."""
        if self.day_avg_seconds is None or self.baseline_avg_seconds is None:
            return None
        return self.day_avg_seconds - self.baseline_avg_seconds

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
        avg = _minutes(self.day_avg_seconds)
        lead = "on track for " if self.in_progress else ""
        delta = self.delta_percent
        if delta is None:
            return f"{self.emoji} {self.route_name}: {lead}{avg:.0f} min today"
        direction = "slower" if delta >= 0 else "faster"
        return (
            f"{self.emoji} {self.route_name}: {lead}{avg:.0f} min, "
            f"{abs(delta):.0f}% {direction}"
        )

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
        if self.in_progress:
            line = f"On track for {_minutes(self.day_avg_seconds):.1f} min over the whole window"
            if self.curve_scale is not None:
                pct = 100 * (self.curve_scale - 1)
                side = "above" if pct >= 0 else "below"
                line += f", running {abs(pct):.0f}% {side} the usual curve"
            lines.append(line)
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
            verb = "is heading for" if self.in_progress else "is"
            lines.append(f"Today {verb} {sign}{abs(_minutes(delta)):.1f} min vs normal")
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
            "projected_avg_seconds": self.projected_avg_seconds,
            "curve_scale": self.curve_scale,
            "projection": list(self.projection),
            "in_progress": self.in_progress,
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


def _clock_minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")[:2]
    return int(hours) * 60 + int(minutes)


def _mean_by_time(rows) -> dict[str, float]:
    grouped: dict[str, list[int]] = {}
    for row in rows:
        grouped.setdefault(row["local_time"], []).append(row["duration_seconds"])
    return {key: mean(values) for key, values in grouped.items()}


@dataclass(frozen=True)
class Projection:
    avg_seconds: float
    # Today's samples over the usual curve at the same times: 1.15 is a day
    # running 15% heavier than normal.
    scale: float
    points: tuple[dict, ...]


# Quartic: enough to bend with a peak that builds and then eases inside the
# window, without chasing the noise in any single clock time's average.
CURVE_DEGREE = 4


def fit_curve(profile: dict[str, float], degree: int = CURVE_DEGREE) -> dict[str, float]:
    """Least-squares polynomial through the usual duration at each clock time.

    Returns the smoothed value at each of the profile's own times, which is the
    only place the curve is ever read: a quartic is well behaved between the
    points it was fitted to and wild beyond them, and nothing here asks it to
    extrapolate. The degree drops to fit a profile with only a few times, where
    it then passes through every point.
    """
    keys = sorted(profile, key=_clock_minutes)
    degree = min(degree, len(keys) - 1)
    if degree < 1:
        return dict(profile)
    xs = np.array([_clock_minutes(key) for key in keys], dtype=float)
    # Polynomial.fit maps the clock times onto [-1, 1] before fitting; raw
    # minutes-since-midnight to the fourth power would be ill-conditioned.
    curve = Polynomial.fit(xs, [profile[key] for key in keys], degree)
    return {key: float(value) for key, value in zip(keys, curve(xs), strict=True)}


def project_day(profile: dict[str, float], today: dict[str, float]) -> Projection | None:
    """Project today's whole-window average from the samples taken so far.

    ``profile`` is the usual duration at each clock time; ``today`` is what each
    clock time measured today. A smooth curve is fitted to the profile, then
    scaled by the single factor that best matches today's samples (least
    squares through the origin), and read off at the times still to come.

    Scaling rather than shifting is what carries the rate of change: a day at
    1.2x the usual curve is also climbing 1.2x as steeply, so a morning that is
    getting worse faster than normal projects a gap that keeps widening --
    exactly as far as the usual curve climbs, and no further.

    Returns None when no measured time has a usual value to compare against.
    """
    curve = fit_curve(profile)
    pairs = [(curve[key], seconds) for key, seconds in today.items() if key in curve]
    denominator = sum(usual * usual for usual, _ in pairs)
    if not pairs or denominator <= 0:
        return None
    scale = sum(usual * seconds for usual, seconds in pairs) / denominator

    points = []
    for key in sorted(set(curve) | set(today), key=_clock_minutes):
        if key in today:
            points.append({"local_time": key, "seconds": today[key], "measured": True})
        else:
            projected = max(0.0, scale * curve[key])
            points.append({"local_time": key, "seconds": projected, "measured": False})
    return Projection(
        avg_seconds=mean(point["seconds"] for point in points),
        scale=scale,
        points=tuple(points),
    )


def _in_window(route: Route, hhmm: str) -> bool:
    hours, minutes = (int(part) for part in hhmm.split(":")[:2])
    return route.window_start <= time(hours, minutes) <= route.window_end


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
    so a day with more samples than usual does not dominate it. Today is scored
    on its projected whole-window average (see :func:`project_day`), which
    becomes its actual average once the window is over.
    """
    today = today or datetime.now(route.tzinfo).date()
    today_iso = today.isoformat()
    since_iso = (today - timedelta(days=baseline_days)).isoformat()

    # Explicit date bounds rather than a "last N days" filter: the report must
    # describe the day it is asked about, not the day the query happens to run.
    rows = db.daily_stats(route.id, since=since_iso, until=today_iso)
    today_row = next((row for row in rows if row["local_date"] == today_iso), None)
    prior = [row for row in rows if row["local_date"] < today_iso]

    projection = None
    if today_row and len(prior) >= min_baseline_days:
        samples = db.samples(route.id, since=since_iso, until=today_iso)
        # Only times still inside the window: an old schedule's sample times
        # are not part of today's day and must not be projected into it.
        profile = {
            key: value
            for key, value in _mean_by_time(
                row for row in samples if row["local_date"] < today_iso
            ).items()
            if _in_window(route, key)
        }
        measured = _mean_by_time(row for row in samples if row["local_date"] == today_iso)
        projection = project_day(profile, measured)

    baseline_avg = baseline_sd = z_score = None
    if len(prior) >= min_baseline_days:
        averages = [row["avg_seconds"] for row in prior]
        baseline_avg = mean(averages)
        baseline_sd = stdev(averages) if len(averages) > 1 else 0.0
        day_avg = None
        if projection:
            day_avg = projection.avg_seconds
        elif today_row:
            day_avg = today_row["avg_seconds"]
        if day_avg is not None and baseline_sd:
            z_score = (day_avg - baseline_avg) / baseline_sd
        elif day_avg is not None:
            # A perfectly steady baseline: any movement at all is the signal.
            difference = day_avg - baseline_avg
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
        projected_avg_seconds=projection.avg_seconds if projection else None,
        curve_scale=projection.scale if projection else None,
        projection=projection.points if projection else (),
    )
