"""SQLite storage for commute samples.

One row per successful lookup, plus a table of failures so gaps in a chart can
be explained. Connections are opened per call -- the write volume here is a
handful of rows per day, and it keeps the scheduler and web threads independent.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id                TEXT    NOT NULL,
    route_name              TEXT    NOT NULL,
    origin                  TEXT    NOT NULL,
    destination             TEXT    NOT NULL,
    sampled_at_utc          TEXT    NOT NULL,
    local_date              TEXT    NOT NULL,
    local_time              TEXT    NOT NULL,
    weekday                 TEXT    NOT NULL,
    duration_seconds        INTEGER NOT NULL,
    static_duration_seconds INTEGER,
    distance_meters         INTEGER
);

CREATE INDEX IF NOT EXISTS idx_samples_route_date
    ON samples (route_id, local_date);
CREATE INDEX IF NOT EXISTS idx_samples_route_time
    ON samples (route_id, local_time);

CREATE TABLE IF NOT EXISTS failures (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id         TEXT NOT NULL,
    attempted_at_utc TEXT NOT NULL,
    local_date       TEXT NOT NULL,
    local_time       TEXT NOT NULL,
    message          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_failures_route_date
    ON failures (route_id, local_date);

-- Small key/value store for facts about the database itself, such as whether
-- the config files have already been imported.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Routes are edited from the dashboard, so the database is their source of
-- truth. .env / routes.yml seed this table on first run and are not consulted
-- again; deleting a route leaves its samples behind on purpose.
CREATE TABLE IF NOT EXISTS routes (
    id               TEXT    PRIMARY KEY,
    name             TEXT    NOT NULL,
    origin           TEXT    NOT NULL,
    destination      TEXT    NOT NULL,
    window_start     TEXT    NOT NULL,
    window_end       TEXT    NOT NULL,
    interval_minutes INTEGER NOT NULL,
    days             TEXT    NOT NULL,
    timezone         TEXT    NOT NULL,
    notify_at        TEXT,
    notify_days      TEXT,
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
"""


class Database:
    """All reads and writes against the sample store."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------------------------------------------------------------- writes

    def record_sample(
        self,
        *,
        route_id: str,
        route_name: str,
        origin: str,
        destination: str,
        local_dt: datetime,
        duration_seconds: int,
        static_duration_seconds: int | None = None,
        distance_meters: int | None = None,
    ) -> int:
        """Store one measurement. ``local_dt`` must be timezone-aware."""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO samples (
                    route_id, route_name, origin, destination, sampled_at_utc,
                    local_date, local_time, weekday, duration_seconds,
                    static_duration_seconds, distance_meters
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    route_id,
                    route_name,
                    origin,
                    destination,
                    local_dt.astimezone(UTC).isoformat(timespec="seconds"),
                    local_dt.strftime("%Y-%m-%d"),
                    local_dt.strftime("%H:%M"),
                    local_dt.strftime("%a").lower(),
                    duration_seconds,
                    static_duration_seconds,
                    distance_meters,
                ),
            )
            return int(cursor.lastrowid)

    def record_failure(self, *, route_id: str, local_dt: datetime, message: str) -> None:
        """Store a failed lookup so gaps in the data are explainable."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO failures (route_id, attempted_at_utc, local_date, local_time, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    route_id,
                    local_dt.astimezone(UTC).isoformat(timespec="seconds"),
                    local_dt.strftime("%Y-%m-%d"),
                    local_dt.strftime("%H:%M"),
                    message[:1000],
                ),
            )

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value))
            )

    # ---------------------------------------------------------------- routes

    def list_route_rows(self) -> list[dict]:
        """Every configured route, newest name order, enabled or not."""
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM routes ORDER BY name").fetchall()
        return [dict(row) for row in rows]

    def get_route_row(self, route_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
        return dict(row) if row else None

    def save_route_row(self, row: dict) -> None:
        """Insert or replace one route, stamping created_at/updated_at."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        existing = self.get_route_row(row["id"])
        payload = {
            **row,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }
        columns = [
            "id",
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
            "created_at",
            "updated_at",
        ]
        placeholders = ", ".join("?" for _ in columns)
        with self.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO routes ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(payload[column] for column in columns),
            )

    def delete_route_row(self, route_id: str) -> bool:
        """Remove a route. Its collected samples are deliberately left in place."""
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM routes WHERE id = ?", (route_id,))
            return cursor.rowcount > 0

    def route_id_exists(self, route_id: str) -> bool:
        with self.connect() as conn:
            return (
                conn.execute("SELECT 1 FROM routes WHERE id = ?", (route_id,)).fetchone()
                is not None
            )

    def delete_samples(self, route_id: str) -> int:
        """Erase a route's collected history. Only called when explicitly asked."""
        with self.connect() as conn:
            deleted = conn.execute("DELETE FROM samples WHERE route_id = ?", (route_id,)).rowcount
            conn.execute("DELETE FROM failures WHERE route_id = ?", (route_id,))
        return deleted

    # ----------------------------------------------------------------- reads

    def tracked_routes(self) -> list[dict]:
        """Every route that has at least one stored sample."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT route_id,
                       MAX(route_name)  AS route_name,
                       MAX(origin)      AS origin,
                       MAX(destination) AS destination,
                       COUNT(*)         AS samples
                FROM samples
                GROUP BY route_id
                ORDER BY route_name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self, route_id: str, *, days: int | None = None) -> dict:
        """Min / max / average / median across the tracked history."""
        where, params = self._window(route_id, days)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*)                   AS samples,
                       COUNT(DISTINCT local_date) AS days_tracked,
                       MIN(duration_seconds)      AS min_seconds,
                       MAX(duration_seconds)      AS max_seconds,
                       AVG(duration_seconds)      AS avg_seconds,
                       AVG(distance_meters)       AS avg_distance_meters,
                       MIN(local_date)            AS first_date,
                       MAX(local_date)            AS last_date
                FROM samples {where}
                """,
                params,
            ).fetchone()
            durations = [
                r[0]
                for r in conn.execute(
                    f"SELECT duration_seconds FROM samples {where} ORDER BY duration_seconds",
                    params,
                ).fetchall()
            ]
            # The failures table shares the route_id/local_date columns, so the
            # same WHERE clause applies unchanged.
            failures = conn.execute(
                f"SELECT COUNT(*) FROM failures {where}", params
            ).fetchone()[0]

        result = dict(row)
        result["route_id"] = route_id
        result["median_seconds"] = float(median(durations)) if durations else None
        result["p90_seconds"] = _percentile(durations, 0.90)
        result["failures"] = failures
        return result

    def daily_stats(
        self,
        route_id: str,
        *,
        days: int | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict]:
        """One row per day: min, max, average and sample count.

        ``days`` is relative to today; ``since``/``until`` are explicit ``YYYY-MM-DD``
        bounds (inclusive), which is what the daily report needs so its baseline
        does not depend on the wall clock at the moment of the query.
        """
        where, params = self._window(route_id, days, since=since, until=until)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT local_date,
                       MAX(weekday)          AS weekday,
                       COUNT(*)              AS samples,
                       MIN(duration_seconds) AS min_seconds,
                       MAX(duration_seconds) AS max_seconds,
                       AVG(duration_seconds) AS avg_seconds
                FROM samples {where}
                GROUP BY local_date
                ORDER BY local_date
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def time_of_day_stats(self, route_id: str, *, days: int | None = None) -> list[dict]:
        """One row per clock time in the window, aggregated across every day."""
        where, params = self._window(route_id, days)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT local_time,
                       COUNT(*)              AS samples,
                       MIN(duration_seconds) AS min_seconds,
                       MAX(duration_seconds) AS max_seconds,
                       AVG(duration_seconds) AS avg_seconds
                FROM samples {where}
                GROUP BY local_time
                ORDER BY local_time
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def weekday_stats(self, route_id: str, *, days: int | None = None) -> list[dict]:
        """One row per weekday, aggregated across every occurrence of that day."""
        where, params = self._window(route_id, days)
        order = (
            "CASE weekday WHEN 'mon' THEN 1 WHEN 'tue' THEN 2 WHEN 'wed' THEN 3 "
            "WHEN 'thu' THEN 4 WHEN 'fri' THEN 5 WHEN 'sat' THEN 6 ELSE 7 END"
        )
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT weekday,
                       COUNT(*)              AS samples,
                       MIN(duration_seconds) AS min_seconds,
                       MAX(duration_seconds) AS max_seconds,
                       AVG(duration_seconds) AS avg_seconds
                FROM samples {where}
                GROUP BY weekday
                ORDER BY {order}
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def samples(self, route_id: str, *, days: int | None = None, limit: int = 20000) -> list[dict]:
        """Raw samples, oldest first, for scatter plots and CSV export."""
        where, params = self._window(route_id, days)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT local_date, local_time, weekday, sampled_at_utc,
                       duration_seconds, static_duration_seconds, distance_meters
                FROM samples {where}
                ORDER BY local_date, local_time
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_failures(self, route_id: str, limit: int = 20) -> list[dict]:
        """The most recent failed lookups, newest first."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT local_date, local_time, message
                FROM failures WHERE route_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (route_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _window(
        route_id: str,
        days: int | None,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> tuple[str, tuple]:
        """Build the shared WHERE clause for a route over an optional date window."""
        clauses = ["route_id = ?"]
        params: list = [route_id]
        if days:
            clauses.append("local_date >= date('now', ?)")
            params.append(f"-{int(days)} days")
        if since:
            clauses.append("local_date >= ?")
            params.append(since)
        if until:
            clauses.append("local_date <= ?")
            params.append(until)
        return "WHERE " + " AND ".join(clauses), tuple(params)


def _percentile(sorted_values: list[int], fraction: float) -> float | None:
    """Nearest-rank percentile over an already-sorted list."""
    if not sorted_values:
        return None
    index = max(0, min(len(sorted_values) - 1, round(fraction * len(sorted_values)) - 1))
    return float(sorted_values[index])
