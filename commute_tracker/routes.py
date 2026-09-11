"""The route store: ``routes.yml`` is the source of truth.

Every edit made in the dashboard is written straight back to the file, so the
routes survive a restart, a rebuild, or the database being thrown away -- and so
you can read, hand-edit, back up or version-control them like any other config.

The file is re-read whenever it changes on disk, which means a hand edit is
picked up without a restart. Writes are atomic (temp file + replace), so a crash
mid-write cannot leave a half-written file behind.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path

import yaml

from .config import ConfigError, Route, slugify

log = logging.getLogger(__name__)

HEADER = """\
# Commute Tracker routes.
#
# This file is written by the dashboard whenever you add, edit or delete a
# route, and re-read when it changes on disk -- so hand edits are picked up
# without a restart. Keep the `id` of a route to keep its collected history.
"""

class _Dumper(yaml.SafeDumper):
    """A dumper that quotes any string YAML would read back as something else."""


def _represent_str(dumper: yaml.SafeDumper, value: str):
    """Quote strings that are not their own round trip.

    YAML 1.1 reads a bare ``16:30`` as the sexagesimal integer 990, ``no`` as
    False and ``1.0`` as a float -- so a quoted style is required for those or
    the file cannot be loaded back.
    """
    try:
        ambiguous = yaml.safe_load(value) != value
    except yaml.YAMLError:
        ambiguous = True
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="'" if ambiguous else None)


_Dumper.add_representer(str, _represent_str)


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
    """CRUD over ``routes.yml``, in :class:`Route` terms."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cache: list[Route] = []
        self._stamp: tuple[float, int] | None = None

    # ------------------------------------------------------------------ read

    def all(self, *, enabled_only: bool = False) -> list[Route]:
        with self._lock:
            routes = self._load()
        return [r for r in routes if r.enabled] if enabled_only else list(routes)

    def get(self, route_id: str) -> Route | None:
        return next((r for r in self.all() if r.id == route_id), None)

    def _load(self) -> list[Route]:
        """Parse the file, reusing the cache while its mtime and size are unchanged."""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._cache, self._stamp = [], None
            return self._cache

        stamp = (stat.st_mtime, stat.st_size)
        if stamp == self._stamp:
            return self._cache

        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{self.path} should be a mapping with a 'routes:' list")
        defaults = raw.get("defaults") or {}
        entries = raw.get("routes") or []
        routes, seen = [], set()
        for entry in entries:
            route = Route.from_dict(entry, defaults)
            if route.id in seen:
                raise ConfigError(f"Duplicate route id {route.id!r} in {self.path}")
            seen.add(route.id)
            routes.append(route)

        self._cache, self._stamp = routes, stamp
        return routes

    # ----------------------------------------------------------------- write

    def create(self, payload: dict) -> Route:
        """Validate and store a new route, allocating a unique id from its name."""
        with self._lock:
            routes = list(self._load())
            data = _clean(payload)
            taken = {r.id for r in routes}
            route = Route.from_dict(
                {**data, "id": _allocate_id(data.get("name") or "commute", taken)}
            )
            _reject_if_too_expensive(route)
            routes.append(route)
            self._write(routes)
        log.info("[%s] route created: %s -> %s", route.id, route.origin, route.destination)
        return route

    def update(self, route_id: str, payload: dict) -> Route:
        """Apply a partial update. The id never changes, so history stays attached."""
        with self._lock:
            routes = list(self._load())
            index = next((i for i, r in enumerate(routes) if r.id == route_id), None)
            if index is None:
                raise LookupError(route_id)
            merged = {**routes[index].as_dict(), **_clean(payload), "id": route_id}
            route = Route.from_dict(merged)
            _reject_if_too_expensive(route)
            routes[index] = route
            self._write(routes)
        log.info("[%s] route updated", route_id)
        return route

    def delete(self, route_id: str) -> bool:
        """Remove a route from the file. Its samples live in the database, untouched."""
        with self._lock:
            routes = list(self._load())
            remaining = [r for r in routes if r.id != route_id]
            if len(remaining) == len(routes):
                return False
            self._write(remaining)
        log.info("[%s] route deleted", route_id)
        return True

    def _write(self, routes: list[Route]) -> None:
        """Replace the file atomically, so a crash cannot truncate it."""
        document = {"routes": [_to_yaml(route) for route in routes]}
        body = HEADER + "\n" + yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
        # Force a re-read on next access rather than trusting our own in-memory copy.
        self._stamp = None

    # ------------------------------------------------------------- migration

    def migrate_from_database(self, db) -> int:
        """Move routes out of the old routes table into the file, once.

        Earlier versions kept routes in SQLite. If that table still has rows and
        no file exists yet, write them out so nothing is lost on upgrade.
        """
        if self.path.exists():
            return 0
        rows = db.legacy_route_rows()
        if not rows:
            return 0
        with self._lock:
            self._write([Route.from_row(row) for row in rows])
        log.info("Migrated %d route(s) from the database into %s", len(rows), self.path)
        return len(rows)


def _to_yaml(route: Route) -> dict:
    """One route as it appears in the file: quoted times, omitted empties."""
    entry = {
        "id": route.id,
        "name": route.name,
        "origin": route.origin,
        "destination": route.destination,
        "window_start": route.window_start.strftime("%H:%M"),
        "window_end": route.window_end.strftime("%H:%M"),
        "interval_minutes": route.interval_minutes,
        "days": ",".join(route.days),
        "timezone": route.timezone,
        "enabled": route.enabled,
    }
    if route.notify_at:
        entry["notify_at"] = route.notify_at.strftime("%H:%M")
    if route.notify_days:
        entry["notify_days"] = ",".join(route.notify_days)
    return entry


def _reject_if_too_expensive(route: Route) -> None:
    """Apply the cost guards, which gate writes only.

    Deliberately not enforced when a route is *loaded*: a routes.yml written
    before a limit existed (or before it was tightened) must keep working, or
    lowering MIN_INTERVAL_MINUTES would brick the install and take the dashboard
    that is the only place to fix it down too.
    """
    problem = route.cost_guard_error()
    if problem:
        raise ConfigError(problem)


def _allocate_id(name: str, taken: set[str]) -> str:
    """``morning-commute``, then ``-2``, ``-3``... if that name is taken."""
    base = slugify(name)
    candidate, suffix = base, 1
    while candidate in taken:
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
