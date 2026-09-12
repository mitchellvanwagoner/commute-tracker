"""SQLite storage for commute samples.

One row per successful lookup, plus a table of failures so gaps in a chart can
be explained. Connections are opened per call -- the write volume here is a
handful of rows per day, and it keeps the scheduler and web threads independent.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from pathlib import Path
from statistics import median

from .config import ConfigError

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

-- One row per request that actually reached the Routes API, which is the unit
-- Google bills. Kept separate from `samples` because not every billed call
-- produces a sample: the editor's Test button spends one, and a call that comes
-- back 4xx still leaves the meter running.
CREATE TABLE IF NOT EXISTS api_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at_utc TEXT    NOT NULL,
    billing_month TEXT    NOT NULL,
    route_id      TEXT,
    kind          TEXT    NOT NULL,
    ok            INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_api_calls_month ON api_calls (billing_month);
"""

# Routes used to live in this database; they now live in routes.yml. The table
# is only read once, to migrate an existing install, and then dropped.
LEGACY_ROUTES_TABLE = "routes"


class StorageError(ConfigError):
    """The database cannot be opened where it was asked to live.

    A ConfigError, so the CLI reports it as the misconfiguration it is rather
    than as a crash.
    """


def _who_owns(path: Path) -> str:
    """``, owned by uid 99, gid 100``, or empty where the OS has no such notion."""
    if not hasattr(os, "getuid"):  # Windows reports 0 for both; better to say nothing.
        return ""
    try:
        info = path.stat()
    except OSError:
        return ""
    return f", owned by uid {info.st_uid}, gid {info.st_gid}"


def _unwritable(path: Path, blocker: Path, detail: str) -> StorageError:
    # Only Unix has the uids that make this failure make sense; on Windows the
    # clause would read as a sentence with its subject missing.
    running_as = f" We are running as uid {os.getuid()}." if hasattr(os, "getuid") else ""
    return StorageError(
        f"""Cannot open the database at {path}.
  {detail}{_who_owns(blocker)}.{running_as}
  In Docker /data is a mount, so its ownership comes from the host, not from
  the image. On Unraid /mnt/user/appdata is owned by nobody:users: put PUID=99
  and PGID=100 in .env, then recreate the container.
  Otherwise chown the directory to the user the container runs as, or point
  DB_PATH somewhere writable."""
    )


def _ensure_writable(path: Path) -> None:
    """Fail with an explanation, rather than sqlite3's 'unable to open database file'.

    Called before the first connect because that error names neither the path,
    nor the user, nor the permission that was missing -- which is the whole of
    what you need to know to fix it.
    """
    directory = path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _unwritable(
            path, directory, f"{directory} could not be created ({exc.strerror})"
        ) from exc
    # An existing file must itself be writable; otherwise it is the directory
    # that has to allow creating one.
    target = path if path.exists() else directory
    if not os.access(target, os.W_OK):
        raise _unwritable(path, target, f"{target} is not writable")


class Database:
    """All reads and writes against the sample store."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        _ensure_writable(self.path)
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

    # ------------------------------------------------------------ API usage

    def record_api_call(
        self, *, billing_month: str, kind: str, route_id: str | None = None, ok: bool = True
    ) -> None:
        """Tick the meter for one request that reached the Routes API."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO api_calls (called_at_utc, billing_month, route_id, kind, ok)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    billing_month,
                    route_id,
                    kind,
                    int(ok),
                ),
            )

    def count_api_calls(self, billing_month: str, kinds: tuple[str, ...] | None = None) -> int:
        """Billed calls in the given ``YYYY-MM``, optionally only of certain kinds.

        Google meters each SKU against its own free allowance, so a caller
        counting against one allowance must not be shown another SKU's calls.
        """
        sql = "SELECT COUNT(*) FROM api_calls WHERE billing_month = ?"
        params: list = [billing_month]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        with self.connect() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def api_calls_by_kind(
        self, billing_month: str, kinds: tuple[str, ...] | None = None
    ) -> dict[str, int]:
        """Where the month's calls went -- scheduled sampling, a manual test, a lookup."""
        sql = "SELECT kind, COUNT(*) FROM api_calls WHERE billing_month = ?"
        params: list = [billing_month]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        with self.connect() as conn:
            rows = conn.execute(sql + " GROUP BY kind", params).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    # ------------------------------------------------- routes (legacy only)

    def legacy_route_rows(self) -> list[dict]:
        """Rows from the old routes table, or nothing if it was never created."""
        with self.connect() as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (LEGACY_ROUTES_TABLE,),
            ).fetchone()
            if not table:
                return []
            rows = conn.execute(f"SELECT * FROM {LEGACY_ROUTES_TABLE} ORDER BY name").fetchall()
        return [dict(row) for row in rows]

    def drop_legacy_routes_table(self) -> None:
        """Remove the old table once its contents are safely in routes.yml."""
        with self.connect() as conn:
            conn.execute(f"DROP TABLE IF EXISTS {LEGACY_ROUTES_TABLE}")
            conn.execute("DROP TABLE IF EXISTS meta")

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

    def tracked_routes_contains(self, route_id: str) -> bool:
        """Whether any sample was ever recorded for this route id."""
        with self.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM samples WHERE route_id = ? LIMIT 1", (route_id,)
                ).fetchone()
                is not None
            )

    def summary(
        self, route_id: str, *, days: int | None = None, today: date_type | None = None
    ) -> dict:
        """Min / max / average / median across the tracked history."""
        where, params = self._window(route_id, days, today=today)
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
        today: date_type | None = None,
    ) -> list[dict]:
        """One row per day: min, max, average and sample count.

        ``days`` is relative to today; ``since``/``until`` are explicit ``YYYY-MM-DD``
        bounds (inclusive), which is what the daily report needs so its baseline
        does not depend on the wall clock at the moment of the query.
        """
        where, params = self._window(route_id, days, since=since, until=until, today=today)
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

    def time_of_day_stats(
        self, route_id: str, *, days: int | None = None, today: date_type | None = None
    ) -> list[dict]:
        """One row per clock time in the window, aggregated across every day."""
        where, params = self._window(route_id, days, today=today)
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

    def weekday_stats(
        self, route_id: str, *, days: int | None = None, today: date_type | None = None
    ) -> list[dict]:
        """One row per weekday, aggregated across every occurrence of that day."""
        where, params = self._window(route_id, days, today=today)
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

    def samples(
        self,
        route_id: str,
        *,
        days: int | None = None,
        limit: int = 20000,
        today: date_type | None = None,
    ) -> list[dict]:
        """Raw samples, oldest first, for scatter plots and CSV export."""
        where, params = self._window(route_id, days, today=today)
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
        today: date_type | None = None,
    ) -> tuple[str, tuple]:
        """Build the shared WHERE clause for a route over an optional date window.

        ``today`` anchors a ``days`` window, and should be the route's *local*
        date. SQLite's own ``date('now')`` is UTC, and ``local_date`` is not:
        west of Greenwich the two disagree for the last hours of every evening,
        so the same "last 7 days" request would answer with eight days of data
        in the morning and seven after dark. Defaults to the UTC date, which is
        the best guess available when the caller knows of no timezone.
        """
        clauses = ["route_id = ?"]
        params: list = [route_id]
        if days:
            anchor = today or datetime.now(UTC).date()
            clauses.append("local_date >= ?")
            params.append((anchor - timedelta(days=int(days))).isoformat())
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
