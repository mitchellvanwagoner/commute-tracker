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
from .geocode import AddressSuggester
from .maps import BudgetExceededError, MapsError, RoutesClient
from .notify import Notifier, deliver, notifiers_from_env
from .report import Report, build_report
from .routes import RouteStore
from .usage import FREE_TIER_AUTOCOMPLETE, PLACES_KINDS, CallBudget

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
        self.budget = CallBudget(
            self.db,
            limit=settings.free_tier_calls,
            timezone=settings.billing_timezone,
        )
        self.client = RoutesClient(
            settings.api_key,
            timeout=settings.request_timeout,
            traffic_model=settings.traffic_model,
            budget=self.budget,
        )
        self.suggester = AddressSuggester(
            settings.address_provider,
            api_key=settings.api_key,
            timeout=settings.request_timeout,
        )
        # Only metered when the provider actually bills; Photon costs nothing,
        # so counting it would put a meaningless number in front of the user.
        self.places_budget = CallBudget(
            self.db,
            limit=FREE_TIER_AUTOCOMPLETE,
            timezone=settings.billing_timezone,
            kinds=PLACES_KINDS,
            label="Places Autocomplete",
        )
        self.notifiers = notifiers_from_env() if notifiers is None else notifiers
        self.store = RouteStore(settings.routes_file)
        # Upgrade path from the version that kept routes in SQLite.
        if self.store.migrate_from_database(self.db):
            self.db.drop_legacy_routes_table()
        self.scheduler = AsyncIOScheduler()

    @property
    def routes(self) -> list[Route]:
        """Every configured route, enabled or not."""
        return self.store.all()

    def active_routes(self) -> list[Route]:
        return self.store.all(enabled_only=True)

    # --------------------------------------------------------------- sampling

    async def sample_route(self, route: Route) -> dict | None:
        """Look up one driving time and store it. Returns the stored row, or None."""
        local_dt = datetime.now(route.tzinfo)
        try:
            travel = await self.client.travel_time(
                route.origin, route.destination, kind="sample", route_id=route.id
            )
        except BudgetExceededError as exc:
            # Not a failure of the route -- the budget gate stopped it. Recorded
            # all the same, so the gap in the chart carries its own explanation.
            log.warning("[%s] sample skipped: %s", route.id, exc)
            self.db.record_failure(route_id=route.id, local_dt=local_dt, message=str(exc))
            return None
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
        """Sample every configured route once, right now.

        One route's failure never costs another its measurement.
        ``sample_route`` already absorbs a failed lookup, but not a failed
        *write* -- a locked database or a full disk raises straight out of it,
        and an unguarded gather would let that one exception discard the
        results of every route that succeeded.
        """
        routes = self.active_routes()
        results = await asyncio.gather(
            *(self.sample_route(route) for route in routes), return_exceptions=True
        )
        samples = []
        for route, result in zip(routes, results, strict=True):
            if isinstance(result, BaseException):
                log.error("[%s] sampling failed: %s", route.id, result, exc_info=result)
            elif result:
                samples.append(result)
        return samples

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
        """Register jobs for every enabled route. Having none is perfectly valid."""
        routes = self.active_routes()
        if not routes:
            log.info("No routes configured yet -- add one from the dashboard")
        for route in routes:
            self.schedule_route(route)

    def schedule_route(self, route: Route) -> None:
        """Register a cron job for each of one route's sample times."""
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
                id=f"{route.id}:{clock.strftime('%H%M')}",
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

    def unschedule_route(self, route_id: str) -> None:
        """Drop every job belonging to a route.

        Matched on the Route each job carries rather than on its id string. An
        id prefix is not safe to match: ``_allocate_id`` hands out
        ``morning-commute-2`` when ``morning-commute`` is taken, so a prefix
        test would let one route's edit silently unschedule the other's jobs.
        """
        for job in self.scheduler.get_jobs():
            target = job.args[0] if job.args else None
            if isinstance(target, Route) and target.id == route_id:
                job.remove()

    def reschedule_route(self, route_id: str) -> None:
        """Re-register a route after it was edited, added, disabled or deleted.

        Called from the API so an edit takes effect immediately rather than at
        the next restart. A no-op before the scheduler is running.
        """
        if not self.scheduler.running:
            return
        self.unschedule_route(route_id)
        route = self.store.get(route_id)
        if route and route.enabled:
            self.schedule_route(route)

    def _schedule_digest(self, route: Route) -> None:
        """Register the daily report job, if one is both configured and deliverable."""
        if route.notify_at is None:
            return
        if not self.notifiers:
            log.warning(
                "[%s] a daily report time is set but no notifier is configured "
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
            id=f"{route.id}:digest",
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

    async def suggest_addresses(self, query: str) -> list[str]:
        """Address suggestions for the editor, metered only if the provider bills."""
        if not self.suggester.will_request(query):
            # Too short, or the provider is off: no request goes out, so nothing
            # may be booked against the allowance. Counting here would spend a
            # budget the editor could not see being spent -- every keystroke
            # below the minimum length would tick a meter Google never billed,
            # and suggestions would eventually switch themselves off for calls
            # that were never made.
            return []
        if self.suggester.is_billable and self.places_budget.exhausted():
            return []
        results = await self.suggester.suggest(query)
        if self.suggester.is_billable:
            self.places_budget.record(kind="autocomplete")
        return results

    async def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await self.client.aclose()
        await self.suggester.aclose()

    def estimated_calls_per_month(self, routes: list[Route] | None = None) -> int:
        """Rough Routes API call count, for keeping an eye on billing."""
        routes = self.active_routes() if routes is None else routes
        return sum(route.calls_per_month() for route in routes)

    def usage(self) -> dict:
        """The month's meter reading, projected forward at the current schedule."""
        return self.budget.snapshot(self.estimated_calls_per_month())

    def overage_warning(self, pending: Route | None = None) -> str | None:
        """Warn if the schedule -- with ``pending`` applied -- would run past the allowance.

        ``pending`` is the route about to be saved. It replaces the stored
        version of itself so an edit is costed as it will actually run, not
        double-counted alongside the version it is replacing.
        """
        if self.budget.limit <= 0:
            return None
        routes = [r for r in self.active_routes() if pending is None or r.id != pending.id]
        if pending is not None and pending.enabled:
            routes.append(pending)

        snapshot = self.budget.snapshot(self.estimated_calls_per_month(routes))
        limit = snapshot["limit"]
        # Two different overages, and both are worth knowing about:
        #   - this month's, prorated over the days actually left to run;
        #   - a full month's, which is what the schedule costs from now on.
        # A change made late in the month can clear the first and still fail the
        # second, and saying nothing then would be the least useful moment to
        # stay quiet -- the bill simply arrives next month instead.
        over_now = snapshot["projected_over_by"]
        over_sustained = max(0, snapshot["projected_calls_per_month"] - limit)
        if not over_now and not over_sustained:
            return None

        # Compute Routes Pro bills about $10 per 1,000 calls past the free tier.
        def dollars(calls: int) -> str:
            return f"${calls / 1000 * 10:,.2f}"

        if over_now:
            return (
                f"This schedule is projected to reach "
                f"{snapshot['projected_month_end']:,} Routes API calls by the end of "
                f"{snapshot['billing_month']} — {over_now:,} over the {limit:,} free-tier "
                f"allowance, about {dollars(over_now)}. {snapshot['used']:,} calls are already "
                f"spent this month. Sampling stops automatically once the allowance runs out."
            )
        return (
            f"This schedule costs {snapshot['projected_calls_per_month']:,} Routes API calls "
            f"over a full month — {over_sustained:,} more than the {limit:,} free-tier "
            f"allowance, about {dollars(over_sustained)} a month. It fits inside "
            f"{snapshot['billing_month']} only because {snapshot['days_left_in_month']} days "
            f"are left to run; a full month at this rate goes over. Sampling stops "
            f"automatically once the allowance runs out."
        )
