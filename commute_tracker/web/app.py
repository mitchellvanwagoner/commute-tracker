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

from ..config import ConfigError, Settings, load_settings
from ..maps import MapsError
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
                len(tracker.active_routes()),
                tracker.estimated_calls_per_month(),
            )
        yield
        await tracker.shutdown()

    app = FastAPI(title="Commute Tracker", version="0.1.0", lifespan=lifespan)
    app.state.tracker = tracker
    app.state.settings = settings

    def _route_or_404(route_id: str):
        route = tracker.store.get(route_id)
        if route is None:
            raise HTTPException(status_code=404, detail=f"Unknown route {route_id!r}")
        return route

    def _resolve(route_id: str | None) -> str:
        """Default to the first configured route when none is given."""
        if route_id:
            return route_id
        routes = tracker.routes
        if routes:
            return routes[0].id
        raise HTTPException(status_code=404, detail="No routes are configured")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "routes": [r.id for r in tracker.routes]}

    @app.get("/api/routes")
    async def api_routes():
        """Configured routes, annotated with how much data each has."""
        tracked = {row["route_id"]: row for row in tracker.db.tracked_routes()}
        payload = []
        for route in tracker.routes:
            stored = tracked.pop(route.id, {})
            payload.append(
                {**route.as_dict(), "samples": stored.get("samples", 0), "configured": True}
            )
        # A deleted route keeps its history, so it stays visible read-only rather
        # than its data silently vanishing from the dashboard.
        for row in tracked.values():
            payload.append(
                {
                    "id": row["route_id"],
                    "name": row["route_name"],
                    "origin": row["origin"],
                    "destination": row["destination"],
                    "samples": row["samples"],
                    "enabled": False,
                    "configured": False,
                }
            )
        return payload

    @app.post("/api/routes", status_code=201)
    async def api_create_route(payload: dict):
        """Add a route from the dashboard and start tracking it immediately."""
        try:
            route = tracker.store.create(payload)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        tracker.reschedule_route(route.id)
        return route.as_dict()

    @app.patch("/api/routes/{route_id}")
    async def api_update_route(route_id: str, payload: dict):
        """Edit a route in place. Its id, and so its history, is preserved."""
        try:
            route = tracker.store.update(route_id, payload)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown route {route_id!r}") from exc
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        tracker.reschedule_route(route.id)
        return route.as_dict()

    @app.delete("/api/routes/{route_id}")
    async def api_delete_route(route_id: str, drop_history: bool = False):
        """Stop tracking a route. Its samples are kept unless drop_history=true."""
        if not tracker.store.delete(route_id, drop_history=drop_history):
            raise HTTPException(status_code=404, detail=f"Unknown route {route_id!r}")
        tracker.reschedule_route(route_id)
        return {"deleted": route_id, "history_dropped": drop_history}

    @app.post("/api/routes/validate")
    async def api_validate_route(payload: dict):
        """Look up two addresses without saving anything.

        This is what the editor Test button calls: it proves both addresses
        resolve and the API key works before a route is committed.
        """
        origin = str(payload.get("origin", "")).strip()
        destination = str(payload.get("destination", "")).strip()
        if not origin or not destination:
            raise HTTPException(status_code=400, detail="Both addresses are required")
        try:
            travel = await tracker.client.travel_time(origin, destination)
        except MapsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "duration_seconds": travel.duration_seconds,
            "distance_meters": travel.distance_meters,
            "static_duration_seconds": travel.static_duration_seconds,
        }

    @app.get("/api/stats")
    async def api_stats(
        route: str | None = None,
        days: int | None = Query(default=None, ge=1, le=3650),
    ):
        """Everything the dashboard charts, in one request."""
        route_id = _resolve(route)
        db = tracker.db
        configured = tracker.store.get(route_id)
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
