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

log = logging.getLogger(__name__)


class CommuteTracker:
    """Owns the Routes API client, the database and the sampling schedule."""

    def __init__(self, settings: Settings, db: Database | None = None):
        self.settings = settings
        self.db = db or Database(settings.db_path)
        self.client = RoutesClient(
            settings.api_key,
            timeout=settings.request_timeout,
            traffic_model=settings.traffic_model,
        )
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
