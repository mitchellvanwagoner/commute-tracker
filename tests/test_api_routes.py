"""The route-editing API the dashboard drives."""

import pytest
from fastapi.testclient import TestClient

from commute_tracker.config import Route, Settings
from commute_tracker.web.app import create_app

SEED = Route.from_dict(
    {
        "name": "Morning commute",
        "origin": "A St",
        "destination": "B Ave",
        "window_start": "07:00",
        "window_end": "09:00",
        "interval_minutes": 15,
        "days": "mon,tue,wed,thu,fri",
        "timezone": "UTC",
    }
)

NEW_ROUTE = {
    "name": "Evening commute",
    "origin": "B Ave",
    "destination": "A St",
    "window_start": "16:30",
    "window_end": "18:30",
    "interval_minutes": 10,
    "days": ["mon", "wed", "fri"],
    "timezone": "America/Los_Angeles",
    "notify_at": "19:00",
}


@pytest.fixture()
def client(tmp_path):
    settings = Settings(api_key="test-key", routes=[SEED], db_path=tmp_path / "api.db")
    with TestClient(create_app(settings, run_scheduler=False)) as test_client:
        yield test_client


def test_the_env_route_is_seeded_on_first_start(client):
    routes = client.get("/api/routes").json()
    assert [r["id"] for r in routes] == ["morning-commute"]
    assert routes[0]["configured"] is True
    assert routes[0]["enabled"] is True


def test_create_returns_201_and_the_route_starts_being_listed(client):
    response = client.post("/api/routes", json=NEW_ROUTE)
    assert response.status_code == 201
    created = response.json()
    assert created["id"] == "evening-commute"
    assert created["days"] == ["mon", "wed", "fri"]
    assert created["notify_at"] == "19:00"
    assert created["samples_per_day"] == 13

    assert {r["id"] for r in client.get("/api/routes").json()} == {
        "morning-commute",
        "evening-commute",
    }


def test_addresses_can_be_edited(client):
    response = client.patch(
        "/api/routes/morning-commute",
        json={"origin": "New Origin Rd", "destination": "New Destination Way"},
    )
    assert response.status_code == 200
    assert response.json()["origin"] == "New Origin Rd"
    assert client.get("/api/routes").json()[0]["destination"] == "New Destination Way"


def test_the_window_and_days_can_be_edited(client):
    response = client.patch(
        "/api/routes/morning-commute",
        json={"window_start": "06:00", "window_end": "10:00", "days": "sat,sun"},
    )
    updated = response.json()
    assert updated["window_start"] == "06:00"
    assert updated["days"] == ["sat", "sun"]
    assert updated["samples_per_day"] == 17


def test_a_route_can_be_paused_and_resumed(client):
    def set_enabled(value):
        return client.patch("/api/routes/morning-commute", json={"enabled": value}).json()

    assert set_enabled(False)["enabled"] is False
    assert client.get("/api/routes").json()[0]["enabled"] is False
    assert set_enabled(True)["enabled"] is True


def test_invalid_edits_are_rejected_with_a_readable_message(client):
    response = client.patch("/api/routes/morning-commute", json={"window_end": "06:00"})
    assert response.status_code == 400
    assert "must be after" in response.json()["detail"]
    # The stored route is untouched.
    assert client.get("/api/routes").json()[0]["window_end"] == "09:00"


def test_unknown_fields_are_rejected_rather_than_silently_dropped(client):
    response = client.patch("/api/routes/morning-commute", json={"origin_address": "A St"})
    assert response.status_code == 400
    assert "Unknown field" in response.json()["detail"]


def test_editing_a_missing_route_is_404(client):
    assert client.patch("/api/routes/nope", json={"name": "x"}).status_code == 404


def test_delete_keeps_history_and_the_route_stays_visible(client):
    client.post("/api/routes", json=NEW_ROUTE)
    assert client.delete("/api/routes/evening-commute").json()["history_dropped"] is False
    assert [r["id"] for r in client.get("/api/routes").json()] == ["morning-commute"]


def test_delete_with_drop_history_removes_the_samples_too(client):
    response = client.delete("/api/routes/morning-commute?drop_history=true")
    assert response.json() == {"deleted": "morning-commute", "history_dropped": True}
    assert client.get("/api/routes").json() == []


def test_deleting_a_missing_route_is_404(client):
    assert client.delete("/api/routes/nope").status_code == 404


def test_validate_requires_both_addresses(client):
    response = client.post("/api/routes/validate", json={"origin": "A St"})
    assert response.status_code == 400
    assert "Both addresses" in response.json()["detail"]


def test_validate_surfaces_the_maps_error_rather_than_a_500(client):
    # The fixture key is not a real one, so the lookup fails; the editor needs
    # that message, not an opaque server error.
    response = client.post(
        "/api/routes/validate", json={"origin": "A St", "destination": "B Ave"}
    )
    assert response.status_code == 400
    assert response.json()["detail"]
