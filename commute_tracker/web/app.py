"""FastAPI app: the dashboard page, the JSON API behind it, and the scheduler.

The scheduler runs inside this process (started in the lifespan hook), so one
container serves the dashboard and collects the samples.
"""

from __future__ import annotations

import csv
import io
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings, load_settings
from ..tracker import CommuteTracker

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None, *, run_scheduler: bool = True) -> FastAPI:
    """Build the ASGI app. ``run_scheduler=False`` is useful in tests."""
    settings = settings or load_settings()
    tracker = CommuteTracker(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_scheduler:
            tracker.start()
            log.info(
                "Tracking %d route(s); ~%d Routes API calls/month",
                len(settings.routes),
                tracker.estimated_calls_per_month(),
            )
        yield
        await tracker.shutdown()

    app = FastAPI(title="Commute Tracker", version="0.1.0", lifespan=lifespan)
    app.state.tracker = tracker
    app.state.settings = settings

    def _route_or_404(route_id: str):
        for route in settings.routes:
            if route.id == route_id:
                return route
        raise HTTPException(status_code=404, detail=f"Unknown route {route_id!r}")

    def _resolve(route_id: str | None) -> str:
        """Default to the first configured route when none is given."""
        if route_id:
            return route_id
        if settings.routes:
            return settings.routes[0].id
        raise HTTPException(status_code=404, detail="No routes are configured")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "routes": [r.id for r in settings.routes]}

    @app.get("/api/routes")
    async def api_routes():
        """Configured routes, annotated with how much data each has."""
        tracked = {row["route_id"]: row for row in tracker.db.tracked_routes()}
        payload = []
        for route in settings.routes:
            stored = tracked.pop(route.id, {})
            payload.append(
                {
                    "id": route.id,
                    "name": route.name,
                    "origin": route.origin,
                    "destination": route.destination,
                    "window_start": route.window_start.strftime("%H:%M"),
                    "window_end": route.window_end.strftime("%H:%M"),
                    "interval_minutes": route.interval_minutes,
                    "days": route.days,
                    "timezone": route.timezone,
                    "samples": stored.get("samples", 0),
                    "configured": True,
                }
            )
        # Routes that were tracked previously but are no longer configured still
        # have history worth looking at.
        for row in tracked.values():
            payload.append(
                {
                    "id": row["route_id"],
                    "name": row["route_name"],
                    "origin": row["origin"],
                    "destination": row["destination"],
                    "samples": row["samples"],
                    "configured": False,
                }
            )
        return payload

    @app.get("/api/stats")
    async def api_stats(
        route: str | None = None,
        days: int | None = Query(default=None, ge=1, le=3650),
    ):
        """Everything the dashboard charts, in one request."""
        route_id = _resolve(route)
        db = tracker.db
        configured = next((r for r in settings.routes if r.id == route_id), None)
        return {
            "route_id": route_id,
            "days": days,
            "report": tracker.report_for(configured).as_dict() if configured else None,
            "summary": db.summary(route_id, days=days),
            "daily": db.daily_stats(route_id, days=days),
            "time_of_day": db.time_of_day_stats(route_id, days=days),
            "weekday": db.weekday_stats(route_id, days=days),
            "failures": db.recent_failures(route_id),
        }

    @app.get("/api/samples")
    async def api_samples(
        route: str | None = None,
        days: int | None = Query(default=None, ge=1, le=3650),
    ):
        return tracker.db.samples(_resolve(route), days=days)

    @app.get("/api/samples.csv")
    async def api_samples_csv(
        route: str | None = None,
        days: int | None = Query(default=None, ge=1, le=3650),
    ):
        """Download the raw samples for a route as CSV."""
        route_id = _resolve(route)
        rows = tracker.db.samples(route_id, days=days)
        buffer = io.StringIO()
        columns = [
            "local_date",
            "local_time",
            "weekday",
            "sampled_at_utc",
            "duration_seconds",
            "static_duration_seconds",
            "distance_meters",
        ]
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{route_id}-commutes.csv"'},
        )

    @app.post("/api/sample")
    async def api_sample_now(route: str | None = None):
        """Take a sample right now -- handy for verifying the API key works."""
        result = await tracker.sample_route(_route_or_404(_resolve(route)))
        if result is None:
            raise HTTPException(status_code=502, detail="Lookup failed; see /api/stats failures")
        return result

    @app.get("/api/report")
    async def api_report(route: str | None = None):
        """Today's commute scored against the trailing baseline."""
        return tracker.report_for(_route_or_404(_resolve(route))).as_dict()

    @app.post("/api/notify/test")
    async def api_notify_test(route: str | None = None):
        """Push today's report right now -- for checking notifier setup."""
        target = _route_or_404(_resolve(route))
        if not tracker.notifiers:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No notifier configured; set NTFY_TOPIC "
                    "and/or PUSHOVER_TOKEN + PUSHOVER_USER"
                ),
            )
        return {
            "report": tracker.report_for(target).as_dict(),
            "delivery": await tracker.send_digest(target),
        }

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
