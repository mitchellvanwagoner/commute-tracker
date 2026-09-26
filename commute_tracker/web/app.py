"""FastAPI app: the dashboard page, the JSON API behind it, and the scheduler.

The scheduler runs inside this process (started in the lifespan hook), so one
container serves the dashboard and collects the samples.
"""

from __future__ import annotations

import csv
import io
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import ConfigError, Route, Settings, load_settings
from ..geocode import GeocodeError
from ..maps import MapsError
from ..routes import EDITABLE
from ..tracker import CommuteTracker

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class _RevalidatingStatic(StaticFiles):
    """Serve static files with ``Cache-Control: no-cache``.

    Starlette sends ETag and Last-Modified but no Cache-Control, which leaves a
    browser free to apply heuristic freshness -- caching an asset for a stretch
    proportional to its age and never asking whether it changed. The result is a
    dashboard running last edit's JavaScript with no indication anything is
    stale, which is a genuinely awful thing to debug.

    ``no-cache`` does not mean "do not cache": it means "revalidate first", and
    the ETag already there answers that with a cheap 304 when nothing changed.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _clean_preview(payload: dict) -> dict:
    """Keep only the fields a Route is built from, so a stray key is a 400 not a 500."""
    return {key: value for key, value in payload.items() if key in EDITABLE}


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

    app = FastAPI(title="Commute Tracker", version="0.2.0", lifespan=lifespan)
    app.state.tracker = tracker
    app.state.settings = settings

    def _route_or_404(route_id: str):
        route = tracker.store.get(route_id)
        if route is None:
            raise HTTPException(status_code=404, detail=f"Unknown route {route_id!r}")
        return route

    def _local_today(route_id: str):
        """The route's own current date, which anchors every "last N days" window.

        A route deleted from routes.yml keeps its samples and stays queryable,
        and there is no timezone left to ask; None then lets the database fall
        back to UTC.
        """
        route = tracker.store.get(route_id)
        return datetime.now(route.tzinfo).date() if route else None

    def _resolve(route_id: str | None) -> str:
        """Default to the first configured route when none is given."""
        if route_id:
            return route_id
        routes = tracker.routes
        if routes:
            return routes[0].id
        raise HTTPException(status_code=404, detail="No routes are configured")

    # routes.yml is re-read whenever it changes on disk, so a hand edit can put a
    # parse error in front of any request that touches it. Left to FastAPI a
    # ConfigError is a 500 -- an unexplained failure, from an endpoint that has
    # the explanation right there in the exception. Worse, the dashboard is the
    # only place to repair a broken route, so a 500 on the routes endpoints
    # strands the install on exactly the file it cannot load. 400 with the
    # parser's own message keeps the page up and says which line to fix.
    @app.exception_handler(ConfigError)
    async def _config_error(request: Request, exc: ConfigError):
        log.warning("%s: %s", request.url.path, exc)
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/healthz")
    async def healthz():
        """Liveness, which a broken routes.yml does not disprove.

        The container is serving; a config file it cannot parse is a thing to
        report, not to die of. Returning 500 here would fail the image's
        HEALTHCHECK and have the orchestrator restart -- or keep restarting --
        a process that would come up in precisely the same state.
        """
        try:
            return {"status": "ok", "routes": [r.id for r in tracker.routes]}
        except ConfigError as exc:
            return {"status": "ok", "routes": [], "config_error": str(exc)}

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
        # The route is saved either way -- the spend is the user's call to make.
        # What must not happen is it being made without the number in front of them.
        return {**route.as_dict(), "warning": tracker.overage_warning()}

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
        return {**route.as_dict(), "warning": tracker.overage_warning()}

    @app.delete("/api/routes/{route_id}")
    async def api_delete_route(route_id: str, drop_history: bool = False):
        """Stop tracking a route. Its samples are kept unless drop_history=true."""
        removed = tracker.store.delete(route_id)
        # A route may be gone from routes.yml while its samples remain, so
        # dropping history has to work for those too.
        if not removed and not tracker.db.tracked_routes_contains(route_id):
            raise HTTPException(status_code=404, detail=f"Unknown route {route_id!r}")
        if drop_history:
            tracker.db.delete_samples(route_id)
        tracker.reschedule_route(route_id)
        return {"deleted": route_id, "history_dropped": drop_history}

    @app.get("/api/usage")
    async def api_usage():
        """The month's Routes API meter, projected forward at the current schedule."""
        return tracker.usage()

    @app.post("/api/routes/preview")
    async def api_preview_route(payload: dict):
        """Cost a route before it is saved. Spends nothing -- pure arithmetic.

        This is what lets the editor warn *before* the commitment rather than
        after, which is the only point at which the warning is still actionable.
        """
        route_id = str(payload.pop("id", "") or "").strip()
        try:
            fields = {**_clean_preview(payload), "id": route_id or "__preview__"}
            candidate = Route.from_dict(fields)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "samples_per_day": len(candidate.sample_times()),
            "calls_per_month": candidate.calls_per_month(),
            "warning": tracker.overage_warning(candidate),
            "usage": tracker.usage(),
        }

    @app.get("/api/addresses")
    async def api_addresses(q: str = Query(default="", max_length=200)):
        """Address suggestions for the editor. Free unless the provider is Google.

        A failure here is never an error the user must act on -- the address box
        still accepts anything typed into it -- so a dead provider returns an
        empty list rather than a status the editor would have to handle.
        """
        try:
            return {
                "provider": tracker.suggester.provider,
                "suggestions": await tracker.suggest_addresses(q),
            }
        except GeocodeError as exc:
            log.warning("address lookup failed: %s", exc)
            return {"provider": tracker.suggester.provider, "suggestions": [], "error": str(exc)}

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
            travel = await tracker.client.travel_time(origin, destination, kind="validate")
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
        today = datetime.now(configured.tzinfo).date() if configured else None
        return {
            "route_id": route_id,
            "days": days,
            "report": tracker.report_for(configured).as_dict() if configured else None,
            "summary": db.summary(route_id, days=days, today=today),
            "daily": db.daily_stats(route_id, days=days, today=today),
            "time_of_day": db.time_of_day_stats(route_id, days=days, today=today),
            "weekday": db.weekday_stats(route_id, days=days, today=today),
            # Drawn over the time-of-day profile, so today can be read against
            # the usual curve as it happens.
            "today": db.samples(route_id, since=today.isoformat(), until=today.isoformat())
            if today
            else [],
            "failures": db.recent_failures(route_id),
        }

    @app.get("/api/samples")
    async def api_samples(
        route: str | None = None,
        days: int | None = Query(default=None, ge=1, le=3650),
    ):
        route_id = _resolve(route)
        return tracker.db.samples(route_id, days=days, today=_local_today(route_id))

    @app.get("/api/samples.csv")
    async def api_samples_csv(
        route: str | None = None,
        days: int | None = Query(default=None, ge=1, le=3650),
    ):
        """Download the raw samples for a route as CSV."""
        route_id = _resolve(route)
        rows = tracker.db.samples(route_id, days=days, today=_local_today(route_id))
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
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", _RevalidatingStatic(directory=STATIC_DIR), name="static")
    return app
