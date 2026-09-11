"""The route store, whose source of truth is routes.yml on disk."""

from datetime import UTC, time

import pytest
import yaml

from commute_tracker.config import ConfigError, Route
from commute_tracker.db import Database
from commute_tracker.routes import RouteStore

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
def path(tmp_path):
    return tmp_path / "routes.yml"


@pytest.fixture()
def store(path):
    return RouteStore(path)


def read_yaml(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------- persistence


def test_a_missing_file_means_no_routes_not_an_error(store):
    assert store.all() == []
    assert store.get("anything") is None


def test_create_writes_the_route_to_the_file(store, path):
    route = store.create(BASE)

    assert path.exists()
    entry = read_yaml(path)["routes"][0]
    assert entry["id"] == route.id == "morning-commute"
    assert entry["origin"] == "A St"
    assert entry["window_start"] == "07:00"
    assert entry["days"] == "mon,tue,wed,thu,fri"
    assert entry["enabled"] is True


def test_routes_survive_a_restart(store, path):
    store.create(BASE)
    store.create({**BASE, "name": "Evening commute", "origin": "B Ave", "destination": "A St"})

    # A brand new store, as after a reboot: nothing but the file is shared.
    restarted = RouteStore(path)
    assert [r.id for r in restarted.all()] == ["morning-commute", "evening-commute"]
    assert restarted.get("evening-commute").destination == "A St"


def test_edits_are_written_through_to_the_file(store, path):
    route = store.create(BASE)
    store.update(route.id, {"origin": "New Origin Rd", "window_start": "06:30"})

    entry = read_yaml(path)["routes"][0]
    assert entry["origin"] == "New Origin Rd"
    assert entry["window_start"] == "06:30"
    assert RouteStore(path).get(route.id).origin == "New Origin Rd"


def test_delete_removes_it_from_the_file(store, path):
    store.create(BASE)
    store.create({**BASE, "name": "Evening commute"})

    assert store.delete("morning-commute") is True
    assert [e["id"] for e in read_yaml(path)["routes"]] == ["evening-commute"]


def test_deleting_the_last_route_leaves_a_valid_empty_file(store, path):
    store.create(BASE)
    store.delete("morning-commute")

    assert read_yaml(path) == {"routes": []}
    assert RouteStore(path).all() == []


def test_a_hand_edit_is_picked_up_without_a_restart(store, path):
    store.create(BASE)
    document = read_yaml(path)
    document["routes"][0]["destination"] = "Edited By Hand Ave"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    assert store.get("morning-commute").destination == "Edited By Hand Ave"


def test_times_come_back_as_strings_not_numbers(store, path):
    """Whatever the quoting, a time must load as a string rather than an int."""
    store.create({**BASE, "window_start": "07:00", "notify_at": "09:30"})

    entry = read_yaml(path)["routes"][0]
    assert isinstance(entry["window_start"], str)
    assert isinstance(entry["notify_at"], str)
    reloaded = RouteStore(path).get("morning-commute")
    assert reloaded.window_start == time(7, 0)
    assert reloaded.notify_at == time(9, 30)


def test_optional_fields_are_omitted_rather_than_written_as_null(store, path):
    store.create(BASE)
    entry = read_yaml(path)["routes"][0]
    assert "notify_at" not in entry
    assert "notify_days" not in entry


def test_a_failed_write_does_not_leave_a_temp_file_behind(store, path, monkeypatch):
    store.create(BASE)
    monkeypatch.setattr("commute_tracker.routes.os.replace", _boom)
    with pytest.raises(RuntimeError):
        store.create({**BASE, "name": "Evening commute"})

    assert list(path.parent.glob(".routes.yml.*")) == []
    assert [r.id for r in RouteStore(path).all()] == ["morning-commute"]


def _boom(*args, **kwargs):
    raise RuntimeError("disk full")


# ---------------------------------------------------------------------- CRUD


def test_duplicate_names_get_distinct_ids(store):
    ids = [store.create(BASE).id for _ in range(3)]
    assert ids == ["morning-commute", "morning-commute-2", "morning-commute-3"]


def test_update_is_partial_and_keeps_the_id(store):
    route = store.create(BASE)
    updated = store.update(route.id, {"destination": "C Blvd", "interval_minutes": 5})

    assert updated.id == route.id  # the id is what history hangs off
    assert updated.destination == "C Blvd"
    assert updated.interval_minutes == 5
    assert updated.origin == "A St"


def test_renaming_keeps_the_id_so_samples_stay_attached(store, tmp_path):
    from datetime import datetime

    db = Database(tmp_path / "samples.db")
    route = store.create(BASE)
    db.record_sample(
        route_id=route.id,
        route_name=route.name,
        origin=route.origin,
        destination=route.destination,
        local_dt=datetime(2026, 2, 10, 7, 0, tzinfo=UTC),
        duration_seconds=900,
    )
    renamed = store.update(route.id, {"name": "Totally different name"})

    assert renamed.id == "morning-commute"
    assert db.summary(renamed.id)["samples"] == 1


def test_update_can_clear_the_report_time(store):
    route = store.create({**BASE, "notify_at": "09:30"})
    assert route.notify_at == time(9, 30)
    assert store.update(route.id, {"notify_at": ""}).notify_at is None


def test_disabling_keeps_the_route_but_drops_it_from_active(store):
    route = store.create(BASE)
    store.update(route.id, {"enabled": False})

    assert len(store.all()) == 1
    assert store.all(enabled_only=True) == []


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
def test_invalid_payloads_are_rejected(store, path, payload, message):
    with pytest.raises(ConfigError, match=message):
        store.create(payload)
    assert not path.exists()  # nothing half-written


def test_a_malformed_file_is_reported_clearly(store, path):
    path.write_text("routes:\n  - name: no addresses here\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="missing required"):
        store.all()


def test_duplicate_ids_in_a_hand_edited_file_are_rejected(store, path):
    path.write_text(
        yaml.safe_dump(
            {
                "routes": [
                    {"id": "same", "name": "One", "origin": "A", "destination": "B"},
                    {"id": "same", "name": "Two", "origin": "C", "destination": "D"},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Duplicate route id"):
        store.all()


def test_defaults_in_a_hand_written_file_apply_to_every_route(store, path):
    path.write_text(
        yaml.safe_dump(
            {
                "defaults": {"timezone": "America/Los_Angeles", "interval_minutes": 5},
                "routes": [
                    {"name": "One", "origin": "A", "destination": "B"},
                    {"name": "Two", "origin": "C", "destination": "D", "interval_minutes": 30},
                ],
            }
        ),
        encoding="utf-8",
    )
    one, two = store.all()
    assert one.timezone == two.timezone == "America/Los_Angeles"
    assert (one.interval_minutes, two.interval_minutes) == (5, 30)


# ----------------------------------------------------------------- migration


def test_routes_are_migrated_out_of_an_old_database(tmp_path, path):
    db = Database(tmp_path / "legacy.db")
    with db.connect() as conn:
        conn.executescript(
            """
            CREATE TABLE routes (
                id TEXT PRIMARY KEY, name TEXT, origin TEXT, destination TEXT,
                window_start TEXT, window_end TEXT, interval_minutes INTEGER,
                days TEXT, timezone TEXT, notify_at TEXT, notify_days TEXT,
                enabled INTEGER, created_at TEXT, updated_at TEXT
            );
            INSERT INTO routes VALUES ('morning-commute', 'Morning commute', 'A St', 'B Ave',
                '07:00', '09:00', 15, 'mon,tue', 'UTC', '09:30', NULL, 1, 'x', 'x');
            """
        )

    store = RouteStore(path)
    assert store.migrate_from_database(db) == 1
    assert store.get("morning-commute").notify_at == time(9, 30)

    db.drop_legacy_routes_table()
    assert db.legacy_route_rows() == []
    # And it does not run again now that the file exists.
    assert store.migrate_from_database(db) == 0


def test_migration_does_not_overwrite_an_existing_file(tmp_path, path):
    db = Database(tmp_path / "legacy.db")
    store = RouteStore(path)
    store.create(BASE)
    assert store.migrate_from_database(db) == 0


def test_migration_is_a_no_op_without_a_legacy_table(tmp_path, path):
    db = Database(tmp_path / "fresh.db")
    assert RouteStore(path).migrate_from_database(db) == 0
    assert not path.exists()


def test_route_from_dict_ignores_legacy_bookkeeping_columns():
    route = Route.from_dict({**BASE, "id": "x"})
    assert route.id == "x"


def test_an_evening_window_survives_the_round_trip(store, path):
    """A bare 16:30 is the integer 990 in YAML 1.1, which would corrupt the file."""
    store.create(
        {**BASE, "name": "Evening commute", "window_start": "16:30", "window_end": "18:30"}
    )
    reloaded = RouteStore(path).get("evening-commute")
    assert reloaded.window_start == time(16, 30)
    assert reloaded.window_end == time(18, 30)
