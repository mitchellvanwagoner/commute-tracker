"""The route store: routes live in the database so the dashboard can edit them.

``.env`` and ``routes.yml`` seed the table the first time the app starts against
an empty database. After that the database is authoritative and the files are
ignored, so an edit made in the UI is never silently reverted by a restart.
"""

from __future__ import annotations

import logging

from .config import ConfigError, Route, slugify
from .db import Database

log = logging.getLogger(__name__)

# Marks that .env / routes.yml have already been imported.
SEEDED_KEY = "routes_seeded_from_config"

# Fields a caller may set. Anything else in a payload is rejected rather than
# quietly dropped, so a typo'd field never looks like it was saved.
EDITABLE = {
    "name",
    "origin",
    "destination",
    "window_start",
    "window_end",
    "interval_minutes",
    "days",
    "timezone",
    "notify_at",
    "notify_days",
    "enabled",
}


class RouteStore:
    """CRUD over the routes table, in :class:`Route` terms."""

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ read

    def all(self, *, enabled_only: bool = False) -> list[Route]:
        routes = [Route.from_row(row) for row in self.db.list_route_rows()]
        return [route for route in routes if route.enabled] if enabled_only else routes

    def get(self, route_id: str) -> Route | None:
        row = self.db.get_route_row(route_id)
        return Route.from_row(row) if row else None

    # ----------------------------------------------------------------- write

    def create(self, payload: dict) -> Route:
        """Validate and store a new route, allocating a unique id from its name."""
        data = _clean(payload)
        route = Route.from_dict({**data, "id": self._allocate_id(data.get("name") or "commute")})
        self.db.save_route_row(route.to_row())
        log.info("[%s] route created: %s -> %s", route.id, route.origin, route.destination)
        return route

    def update(self, route_id: str, payload: dict) -> Route:
        """Apply a partial update. The id never changes, so history stays attached."""
        existing = self.get(route_id)
        if existing is None:
            raise LookupError(route_id)
        merged = {**existing.as_dict(), **_clean(payload), "id": existing.id}
        route = Route.from_dict(merged)
        self.db.save_route_row(route.to_row())
        log.info("[%s] route updated", route.id)
        return route

    def delete(self, route_id: str, *, drop_history: bool = False) -> bool:
        """Remove a route. Its samples are kept unless ``drop_history`` is set."""
        if not self.db.delete_route_row(route_id):
            return False
        if drop_history:
            removed = self.db.delete_samples(route_id)
            log.info("[%s] route deleted along with %d samples", route_id, removed)
        else:
            log.info("[%s] route deleted; its samples were kept", route_id)
        return True

    # ------------------------------------------------------------- bootstrap

    def seed(self, routes: list[Route]) -> int:
        """Import the config-file routes once. Returns how many landed.

        The fact that seeding happened is recorded rather than inferred from an
        empty table: otherwise deleting your last route in the dashboard would
        resurrect the .env one on the next restart.
        """
        if self.db.get_meta(SEEDED_KEY):
            return 0
        self.db.set_meta(SEEDED_KEY, "1")
        if self.db.list_route_rows():
            return 0
        for route in routes:
            self.db.save_route_row(route.to_row())
        if routes:
            log.info(
                "Seeded %d route(s) from configuration; the dashboard now owns them "
                "and .env/routes.yml will not be re-read",
                len(routes),
            )
        return len(routes)

    def _allocate_id(self, name: str) -> str:
        """``morning-commute``, then ``-2``, ``-3``... if that name is taken."""
        base = slugify(name)
        candidate, suffix = base, 1
        while self.db.route_id_exists(candidate):
            suffix += 1
            candidate = f"{base}-{suffix}"
        return candidate


def _clean(payload: dict) -> dict:
    """Drop empty optional values and reject unknown fields."""
    unknown = set(payload) - EDITABLE - {"id"}
    if unknown:
        raise ConfigError(f"Unknown field(s): {', '.join(sorted(unknown))}")
    cleaned = {}
    for key, value in payload.items():
        if key not in EDITABLE:
            continue
        if key in {"notify_at", "notify_days"} and value in ("", None):
            cleaned[key] = None
        elif isinstance(value, str):
            cleaned[key] = value.strip()
        else:
            cleaned[key] = value
    return cleaned
