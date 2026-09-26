from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from commute_tracker.config import Settings
from commute_tracker.db import Database
from commute_tracker.routes import RouteStore
from commute_tracker.web.app import create_app

TZ = timezone(timedelta(hours=-8))


@pytest.fixture()
def client(tmp_path):
    spec = {
            "name": "Morning commute",
            "origin": "A St",
            "destination": "B Ave",
            "window_start": "07:00",
            "window_end": "08:00",
            "interval_minutes": 30,
            "days": "mon,tue,wed,thu,fri",
            "timezone": "UTC",
    }
    db_path = tmp_path / "test.db"
    routes_file = tmp_path / "routes.yml"
    route = RouteStore(routes_file).create(spec)
    db = Database(db_path)
    for clock, seconds in (("07:00", 600), ("07:30", 1200)):
        hour, minute = (int(p) for p in clock.split(":"))
        db.record_sample(
            route_id=route.id,
            route_name=route.name,
            origin=route.origin,
            destination=route.destination,
            local_dt=datetime(2026, 1, 5, hour, minute, tzinfo=TZ),
            duration_seconds=seconds,
            static_duration_seconds=seconds - 60,
            distance_meters=40000,
        )

    settings = Settings(api_key="test-key", db_path=db_path, routes_file=routes_file)
    with TestClient(create_app(settings, run_scheduler=False)) as test_client:
        yield test_client


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "routes": ["morning-commute"]}


def test_routes_endpoint_describes_the_configured_window(client):
    route = client.get("/api/routes").json()[0]
    assert route["id"] == "morning-commute"
    assert route["window_start"] == "07:00"
    assert route["interval_minutes"] == 30
    assert route["samples"] == 2
    assert route["configured"] is True


def test_stats_defaults_to_the_first_route(client):
    stats = client.get("/api/stats").json()
    assert stats["route_id"] == "morning-commute"
    assert stats["summary"]["avg_seconds"] == 900
    assert [row["local_time"] for row in stats["time_of_day"]] == ["07:00", "07:30"]
    assert stats["daily"][0]["max_seconds"] == 1200


def test_csv_export_has_a_header_and_a_row_per_sample(client):
    response = client.get("/api/samples.csv")
    assert response.status_code == 200
    lines = response.text.strip().splitlines()
    assert lines[0].startswith("local_date,local_time,weekday")
    assert len(lines) == 3


def test_dashboard_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Commute Tracker" in response.text


def test_report_endpoint_returns_today_severity_and_a_maps_link(client):
    report = client.get("/api/report").json()
    assert report["route_id"] == "morning-commute"
    # The fixture's samples are historical, so today has none to score.
    assert report["severity"] == "grey"
    assert report["maps_url"].startswith("https://www.google.com/maps/dir/")
    assert report["color"] == "#898781"


def test_stats_carries_the_same_report_for_the_dashboard_badge(client):
    stats = client.get("/api/stats").json()
    assert stats["report"]["route_id"] == "morning-commute"
    assert stats["report"]["label"]


def test_notify_test_is_refused_when_no_notifier_is_configured(client):
    response = client.post("/api/notify/test")
    assert response.status_code == 400
    assert "NTFY_TOPIC" in response.json()["detail"]


def test_static_assets_must_be_revalidated(client):
    """A cached dashboard running stale JavaScript is near-impossible to diagnose."""
    for path in ("/", "/static/app.js", "/static/routes.js", "/static/styles.css"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-cache", path


# ------------------------------------------------- a routes.yml that will not parse


BROKEN_YAML = """\
routes:
  - id: a
    name: A
    origin: X
    destination: Y
    days: mon,funday
"""


@pytest.fixture()
def broken_client(tmp_path):
    routes_file = tmp_path / "routes.yml"
    routes_file.write_text(BROKEN_YAML, encoding="utf-8")
    settings = Settings(
        api_key="k", db_path=tmp_path / "broken.db", routes_file=routes_file
    )
    return TestClient(create_app(settings, run_scheduler=False), raise_server_exceptions=False)


def test_healthz_survives_an_unparseable_routes_file(broken_client):
    """Liveness is about the process, which a bad config file does not disprove.

    A 500 here fails the image's HEALTHCHECK and has the orchestrator restart a
    container that would come back up in exactly the same state.
    """
    response = broken_client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "funday" in response.json()["config_error"]


def test_routes_endpoint_explains_the_parse_error(broken_client):
    """400 with the parser's message, not an unexplained 500.

    The dashboard is the only place to repair a route, so the endpoints behind
    it must stay loadable and say what is wrong with the file.
    """
    response = broken_client.get("/api/routes")
    assert response.status_code == 400
    assert "funday" in response.json()["detail"]


def test_the_dashboard_shell_still_loads(broken_client):
    assert broken_client.get("/").status_code == 200


def test_stats_carries_todays_samples_for_the_overlay(client):
    """Only today's samples: the fixture's are from January, so there are none."""
    stats = client.get("/api/stats").json()
    assert stats["today"] == []
    assert stats["report"]["projection"] == []
