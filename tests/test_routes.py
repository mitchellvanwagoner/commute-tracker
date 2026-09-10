from datetime import datetime, time, timedelta, timezone

import pytest

from commute_tracker.config import ConfigError, Route
from commute_tracker.db import Database
from commute_tracker.routes import RouteStore

TZ = timezone(timedelta(hours=-8))

BASE = {
    "name": "Morning commute",
    "origin": "A St",
    "destination": "B Ave",
    "window_start": "07:00",
    "window_end": "09:00",
    "interval_minutes": 15,
    "days": "mon,tue,wed,thu,fri",
    "timezone": "UTC",
}


@pytest.fixture()
def store(tmp_path):
    return RouteStore(Database(tmp_path / "routes.db"))


def test_create_allocates_a_slug_id_and_round_trips(store):
    route = store.create(BASE)
    assert route.id == "morning-commute"
    assert store.get("morning-commute") == route
    assert [r.id for r in store.all()] == ["morning-commute"]


def test_duplicate_names_get_distinct_ids(store):
    first = store.create(BASE)
    second = store.create(BASE)
    third = store.create(BASE)
    assert [first.id, second.id, third.id] == [
        "morning-commute",
        "morning-commute-2",
        "morning-commute-3",
    ]


def test_update_is_partial_and_keeps_the_id(store):
    route = store.create(BASE)
    updated = store.update(route.id, {"destination": "C Blvd", "interval_minutes": 5})

    assert updated.id == route.id  # the id is what history hangs off
    assert updated.destination == "C Blvd"
    assert updated.interval_minutes == 5
    assert updated.origin == "A St"
    assert updated.name == "Morning commute"


def test_renaming_does_not_move_the_route_or_orphan_its_samples(store):
    route = store.create(BASE)
    store.db.record_sample(
        route_id=route.id,
        route_name=route.name,
        origin=route.origin,
        destination=route.destination,
        local_dt=datetime(2026, 2, 10, 7, 0, tzinfo=TZ),
        duration_seconds=900,
    )
    renamed = store.update(route.id, {"name": "Totally different name"})

    assert renamed.id == "morning-commute"
    assert store.db.summary(renamed.id)["samples"] == 1


def test_update_can_clear_the_report_time(store):
    route = store.create({**BASE, "notify_at": "09:30"})
    assert route.notify_at == time(9, 30)
    assert store.update(route.id, {"notify_at": ""}).notify_at is None


def test_disabling_keeps_the_route_but_drops_it_from_active(store):
    route = store.create(BASE)
    store.update(route.id, {"enabled": False})

    assert len(store.all()) == 1
    assert store.all(enabled_only=True) == []


def test_delete_keeps_history_by_default(store):
    route = store.create(BASE)
    store.db.record_sample(
        route_id=route.id,
        route_name=route.name,
        origin=route.origin,
        destination=route.destination,
        local_dt=datetime(2026, 2, 10, 7, 0, tzinfo=TZ),
        duration_seconds=900,
    )

    assert store.delete(route.id) is True
    assert store.get(route.id) is None
    assert store.db.summary(route.id)["samples"] == 1


def test_delete_can_drop_history_when_asked(store):
    route = store.create(BASE)
    store.db.record_sample(
        route_id=route.id,
        route_name=route.name,
        origin=route.origin,
        destination=route.destination,
        local_dt=datetime(2026, 2, 10, 7, 0, tzinfo=TZ),
        duration_seconds=900,
    )
    store.db.record_failure(
        route_id=route.id,
        local_dt=datetime(2026, 2, 10, 7, 15, tzinfo=TZ),
        message="boom",
    )

    assert store.delete(route.id, drop_history=True) is True
    assert store.db.summary(route.id)["samples"] == 0
    assert store.db.recent_failures(route.id) == []


def test_delete_of_an_unknown_route_is_false_not_an_error(store):
    assert store.delete("nope") is False


def test_update_of_an_unknown_route_raises_lookup_error(store):
    with pytest.raises(LookupError):
        store.update("nope", {"name": "x"})


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({**BASE, "window_start": "10:00", "window_end": "09:00"}, "must be after"),
        ({**BASE, "days": "funday"}, "Unknown day"),
        ({**BASE, "timezone": "Mars/Olympus_Mons"}, "unknown timezone"),
        ({**BASE, "window_start": "7am"}, "time like"),
        ({**BASE, "origin": ""}, "missing required"),
        ({**BASE, "colour": "red"}, "Unknown field"),
    ],
)
def test_invalid_payloads_are_rejected(store, payload, message):
    with pytest.raises(ConfigError, match=message):
        store.create(payload)


def test_seed_populates_an_empty_table_once(store):
    route = Route.from_dict(BASE)
    assert store.seed([route]) == 1
    assert [r.id for r in store.all()] == [route.id]

    # A second run must not resurrect a route the user deleted in the UI.
    store.delete(route.id)
    assert store.seed([route]) == 0
    assert store.all() == []


def test_seed_does_not_overwrite_edits_made_in_the_ui(store):
    seeded = Route.from_dict(BASE)
    store.seed([seeded])
    store.update(seeded.id, {"origin": "Somewhere else"})

    store.seed([seeded])
    assert store.get(seeded.id).origin == "Somewhere else"
