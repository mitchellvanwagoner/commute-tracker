"""Sampling and scheduling.

Each route gets one cron job per clock time inside its window, generated from
``window_start``/``window_end``/``interval_minutes``. That is more precise than a
single interval job, and it means every day's samples land at the same clock
times -- which is what makes the time-of-day chart comparable across days.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Route, Settings
from .db import Database
from .maps import MapsError, RoutesClient
from .notify import Notifier, deliver, notifiers_from_env
from .report import Report, build_report

log = logging.getLogger(__name__)


class CommuteTracker:
    """Owns the Routes API client, the database, the schedule and the daily digest."""

    def __init__(
        self,
        settings: Settings,
        db: Database | None = None,
        notifiers: list[Notifier] | None = None,
    ):
        self.settings = settings
        self.db = db or Database(settings.db_path)
        self.client = RoutesClient(
            settings.api_key,
            timeout=settings.request_timeout,
            traffic_model=settings.traffic_model,
        )
        self.notifiers = notifiers_from_env() if notifiers is None else notifiers
        self.scheduler = AsyncIOScheduler()

    # --------------------------------------------------------------- sampling

    async def sample_route(self, route: Route) -> dict | None:
        """Look up one driving time and store it. Returns the stored row, or None."""
        local_dt = datetime.now(route.tzinfo)
        try:
            travel = await self.client.travel_time(route.origin, route.destination)
        except MapsError as exc:
            log.error("[%s] lookup failed: %s", route.id, exc)
            self.db.record_failure(route_id=route.id, local_dt=local_dt, message=str(exc))
            return None

        self.db.record_sample(
            route_id=route.id,
            route_name=route.name,
            origin=route.origin,
            destination=route.destination,
            local_dt=local_dt,
            duration_seconds=travel.duration_seconds,
            static_duration_seconds=travel.static_duration_seconds,
            distance_meters=travel.distance_meters,
        )
        log.info(
            "[%s] %s -> %.1f min (%.1f km)",
            route.id,
            local_dt.strftime("%Y-%m-%d %H:%M"),
            travel.duration_seconds / 60,
            (travel.distance_meters or 0) / 1000,
        )
        return {
            "route_id": route.id,
            "local_time": local_dt.strftime("%H:%M"),
            "duration_seconds": travel.duration_seconds,
            "static_duration_seconds": travel.static_duration_seconds,
            "distance_meters": travel.distance_meters,
        }

    async def sample_all(self) -> list[dict]:
        """Sample every configured route once, right now."""
        results = await asyncio.gather(
            *(self.sample_route(route) for route in self.settings.routes)
        )
        return [r for r in results if r]

    # ----------------------------------------------------------- daily digest

    def report_for(self, route: Route) -> Report:
        """Score today's commute for one route against its trailing baseline."""
        options = self.settings.notify
        return build_report(
            self.db,
            route,
            baseline_days=options.baseline_days,
            min_baseline_days=options.min_baseline_days,
            threshold=options.threshold,
        )

    async def send_digest(self, route: Route) -> dict[str, str]:
        """Build today's report for a route and push it to every notifier."""
        report = self.report_for(route)
        return await deliver(report, self.notifiers, timeout=self.settings.request_timeout)

    # -------------------------------------------------------------- schedule

    def schedule(self) -> None:
        """Register a cron job for every sample time of every route."""
        for route in self.settings.routes:
            times = route.sample_times()
            for clock in times:
                self.scheduler.add_job(
                    self.sample_route,
                    CronTrigger(
                        day_of_week=",".join(route.days),
                        hour=clock.hour,
                        minute=clock.minute,
                        timezone=route.tzinfo,
                    ),
                    args=[route],
                    id=f"{route.id}-{clock.strftime('%H%M')}",
                    replace_existing=True,
                    misfire_grace_time=120,
                    max_instances=1,
                )
            log.info(
                "[%s] %s -> %s | %s %s-%s every %dm (%d samples/day, %s)",
                route.id,
                route.origin,
                route.destination,
                ",".join(route.days),
                route.window_start.strftime("%H:%M"),
                route.window_end.strftime("%H:%M"),
                route.interval_minutes,
                len(times),
                route.timezone,
            )
            self._schedule_digest(route)

    def _schedule_digest(self, route: Route) -> None:
        """Register the daily report job, if one is both configured and deliverable."""
        if route.notify_at is None:
            return
        if not self.notifiers:
            log.warning(
                "[%s] NOTIFY_AT is set but no notifier is configured "
                "(set NTFY_TOPIC and/or PUSHOVER_TOKEN + PUSHOVER_USER)",
                route.id,
            )
            return
        if route.notify_at <= route.window_end:
            log.warning(
                "[%s] the daily report at %s fires before the window closes at %s, "
                "so it will summarize a partial day",
                route.id,
                route.notify_at.strftime("%H:%M"),
                route.window_end.strftime("%H:%M"),
            )
        self.scheduler.add_job(
            self.send_digest,
            CronTrigger(
                day_of_week=",".join(route.digest_days),
                hour=route.notify_at.hour,
                minute=route.notify_at.minute,
                timezone=route.tzinfo,
            ),
            args=[route],
            id=f"{route.id}-digest",
            replace_existing=True,
            misfire_grace_time=3600,
            max_instances=1,
        )
        log.info(
            "[%s] daily report at %s on %s via %s",
            route.id,
            route.notify_at.strftime("%H:%M"),
            ",".join(route.digest_days),
            ", ".join(n.name for n in self.notifiers),
        )

    def start(self) -> None:
        self.schedule()
        self.scheduler.start()

    async def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await self.client.aclose()

    def estimated_calls_per_month(self) -> int:
        """Rough Routes API call count, for keeping an eye on billing."""
        total = 0
        for route in self.settings.routes:
            total += len(route.sample_times()) * len(route.days) * 52 // 12
        return total
