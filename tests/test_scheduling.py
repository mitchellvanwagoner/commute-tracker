"""Scheduling: a route's jobs belong to it and to nothing else."""

from __future__ import annotations

import sqlite3

import pytest

from commute_tracker.config import Settings
from commute_tracker.routes import RouteStore
from commute_tracker.tracker import CommuteTracker

SPEC = {
    "origin": "A St",
    "destination": "B Ave",
    "window_start": "07:00",
    "window_end": "08:00",
    "interval_minutes": 30,
    "days": "mon",
    "timezone": "UTC",
}


@pytest.fixture()
def tracker(tmp_path) -> CommuteTracker:
    routes_file = tmp_path / "routes.yml"
    store = RouteStore(routes_file)
    # Two routes whose ids share a prefix -- exactly what _allocate_id produces
    # when the same name is used twice.
    first = store.create({**SPEC, "name": "Morning commute"})
    second = store.create({**SPEC, "name": "Morning commute"})
    assert (first.id, second.id) == ("morning-commute", "morning-commute-2")
    settings = Settings(
        api_key="test-key", db_path=tmp_path / "s.db", routes_file=routes_file
    )
    return CommuteTracker(settings, notifiers=[])


def job_ids(tracker) -> set[str]:
    return {job.id for job in tracker.scheduler.get_jobs()}


def test_unscheduling_one_route_leaves_a_prefix_sharing_sibling_alone(tracker):
    """Regression: 'morning-commute' must not take 'morning-commute-2' with it."""
    tracker.schedule()
    # The window is inclusive of both ends: 07:00, 07:30, 08:00.
    assert len(job_ids(tracker)) == 6  # 2 routes x 3 sample times

    tracker.unschedule_route("morning-commute")
    survivors = job_ids(tracker)

    assert survivors == {
        "morning-commute-2:0700",
        "morning-commute-2:0730",
        "morning-commute-2:0800",
    }


def test_unscheduling_removes_every_job_of_its_own_route(tracker):
    tracker.schedule()
    tracker.unschedule_route("morning-commute-2")
    assert job_ids(tracker) == {
        "morning-commute:0700",
        "morning-commute:0730",
        "morning-commute:0800",
    }


def test_a_digest_job_is_removed_with_its_route(tmp_path):
    routes_file = tmp_path / "routes.yml"
    store = RouteStore(routes_file)
    store.create({**SPEC, "name": "Commute", "notify_at": "09:00"})
    settings = Settings(
        api_key="k", db_path=tmp_path / "d.db", routes_file=routes_file
    )

    class _Notifier:
        name = "stub"

    tracker = CommuteTracker(settings, notifiers=[_Notifier()])
    tracker.schedule()
    assert "commute:digest" in job_ids(tracker)

    tracker.unschedule_route("commute")
    assert job_ids(tracker) == set()


def test_one_route_failing_to_store_does_not_lose_the_others(tmp_path):
    """A failed *write* is not a failed lookup, and sample_route does not catch it.

    Without return_exceptions the single raise propagates out of the gather and
    discards the measurements every other route just made.
    """
    import asyncio

    from commute_tracker.config import Settings
    from commute_tracker.maps import TravelTime
    from commute_tracker.routes import RouteStore
    from commute_tracker.tracker import CommuteTracker

    routes_file = tmp_path / "routes.yml"
    store = RouteStore(routes_file)
    for name in ("Alpha", "Bravo", "Charlie"):
        store.create(
            {
                "name": name,
                "origin": "A St",
                "destination": "B Ave",
                "window_start": "07:00",
                "window_end": "08:00",
                "interval_minutes": 30,
                "days": "mon",
                "timezone": "UTC",
            }
        )
    settings = Settings(api_key="k", db_path=tmp_path / "s.db", routes_file=routes_file)
    tracker = CommuteTracker(settings, notifiers=[])

    async def travel_time(origin, destination, *, kind="sample", route_id=None):
        return TravelTime(duration_seconds=600, static_duration_seconds=540, distance_meters=1000)

    tracker.client.travel_time = travel_time
    real_record = tracker.db.record_sample

    def record(**kwargs):
        if kwargs["route_id"] == "bravo":
            raise sqlite3.OperationalError("database is locked")
        return real_record(**kwargs)

    tracker.db.record_sample = record

    results = asyncio.run(tracker.sample_all())
    assert sorted(r["route_id"] for r in results) == ["alpha", "charlie"]
