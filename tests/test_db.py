from datetime import datetime, timedelta, timezone

import pytest

from commute_tracker.config import ConfigError
from commute_tracker.db import Database, StorageError

TZ = timezone(timedelta(hours=-8))


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "test.db")


def add(db, *, date, clock, seconds, route="commute"):
    hour, minute = (int(part) for part in clock.split(":"))
    year, month, day = (int(part) for part in date.split("-"))
    db.record_sample(
        route_id=route,
        route_name="Commute",
        origin="A St",
        destination="B Ave",
        local_dt=datetime(year, month, day, hour, minute, tzinfo=TZ),
        duration_seconds=seconds,
        static_duration_seconds=seconds - 60,
        distance_meters=40000,
    )


def test_summary_reports_min_max_average_and_median(db):
    for seconds in (600, 900, 1200, 1500):
        add(db, date="2026-01-05", clock="07:00", seconds=seconds)

    summary = db.summary("commute")
    assert summary["samples"] == 4
    assert summary["days_tracked"] == 1
    assert summary["min_seconds"] == 600
    assert summary["max_seconds"] == 1500
    assert summary["avg_seconds"] == 1050
    assert summary["median_seconds"] == 1050
    assert summary["failures"] == 0


def test_summary_of_an_untracked_route_is_empty_not_an_error(db):
    summary = db.summary("nope")
    assert summary["samples"] == 0
    assert summary["min_seconds"] is None
    assert summary["median_seconds"] is None


def test_daily_stats_group_by_local_date(db):
    add(db, date="2026-01-05", clock="07:00", seconds=600)
    add(db, date="2026-01-05", clock="07:30", seconds=1200)
    add(db, date="2026-01-06", clock="07:00", seconds=900)

    daily = db.daily_stats("commute")
    assert [row["local_date"] for row in daily] == ["2026-01-05", "2026-01-06"]
    assert daily[0] == {
        "local_date": "2026-01-05",
        "weekday": "mon",
        "samples": 2,
        "min_seconds": 600,
        "max_seconds": 1200,
        "avg_seconds": 900,
    }


def test_time_of_day_stats_group_across_days(db):
    add(db, date="2026-01-05", clock="07:00", seconds=600)
    add(db, date="2026-01-06", clock="07:00", seconds=1000)
    add(db, date="2026-01-06", clock="07:30", seconds=1400)

    by_time = db.time_of_day_stats("commute")
    assert [row["local_time"] for row in by_time] == ["07:00", "07:30"]
    assert by_time[0]["min_seconds"] == 600
    assert by_time[0]["max_seconds"] == 1000
    assert by_time[0]["samples"] == 2


def test_weekday_stats_are_ordered_monday_first(db):
    add(db, date="2026-01-09", clock="07:00", seconds=900)  # Friday
    add(db, date="2026-01-05", clock="07:00", seconds=600)  # Monday

    assert [row["weekday"] for row in db.weekday_stats("commute")] == ["mon", "fri"]


def test_routes_are_kept_separate(db):
    add(db, date="2026-01-05", clock="07:00", seconds=600, route="morning")
    add(db, date="2026-01-05", clock="17:00", seconds=1800, route="evening")

    assert db.summary("morning")["max_seconds"] == 600
    assert db.summary("evening")["max_seconds"] == 1800
    assert {row["route_id"] for row in db.tracked_routes()} == {"morning", "evening"}


def test_failures_are_recorded_and_counted(db):
    add(db, date="2026-01-05", clock="07:00", seconds=600)
    db.record_failure(
        route_id="commute",
        local_dt=datetime(2026, 1, 5, 7, 15, tzinfo=TZ),
        message="Routes API returned HTTP 429",
    )

    assert db.summary("commute")["failures"] == 1
    failures = db.recent_failures("commute")
    assert failures[0]["local_time"] == "07:15"
    assert "429" in failures[0]["message"]


def test_samples_come_back_oldest_first(db):
    add(db, date="2026-01-06", clock="07:00", seconds=900)
    add(db, date="2026-01-05", clock="07:30", seconds=600)

    rows = db.samples("commute")
    assert [(r["local_date"], r["local_time"]) for r in rows] == [
        ("2026-01-05", "07:30"),
        ("2026-01-06", "07:00"),
    ]
    assert rows[0]["static_duration_seconds"] == 540


# ------------------------------------------------------- unwritable locations
#
# sqlite reports "unable to open database file" and names neither the path, the
# user, nor the missing permission -- which on a NAS, where /data is a mount
# owned by the host, is the whole of what you need to know. See _ensure_writable.


def test_a_blocked_parent_directory_is_reported_with_the_path(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")

    with pytest.raises(StorageError) as caught:
        Database(blocker / "sub" / "commutes.db")

    message = str(caught.value)
    assert str(blocker) in message
    assert "PUID" in message, "the message must name the fix on Unraid"


def test_a_storage_error_is_a_config_error(tmp_path):
    """So the CLI reports it as a misconfiguration rather than as a crash."""
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")

    with pytest.raises(ConfigError):
        Database(blocker / "commutes.db")


def test_a_missing_parent_directory_is_created(tmp_path):
    db = Database(tmp_path / "nested" / "deeper" / "commutes.db")
    assert db.path.exists()
