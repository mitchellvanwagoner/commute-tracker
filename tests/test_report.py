from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from commute_tracker.config import Route
from commute_tracker.db import Database
from commute_tracker.report import build_report, classify, directions_url

TZ = timezone(timedelta(hours=-8))


@pytest.fixture()
def route():
    return Route.from_dict(
        {
            "name": "Morning commute",
            "origin": "1600 Amphitheatre Pkwy, Mountain View, CA",
            "destination": "1 Ferry Building, San Francisco, CA",
            "window_start": "07:00",
            "window_end": "09:00",
            "timezone": "UTC",
        }
    )


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "report.db")


def add_day(db, route, day: date, seconds_list):
    for index, seconds in enumerate(seconds_list):
        db.record_sample(
            route_id=route.id,
            route_name=route.name,
            origin=route.origin,
            destination=route.destination,
            local_dt=datetime(day.year, day.month, day.day, 7, 15 * index, tzinfo=TZ),
            duration_seconds=seconds,
            distance_meters=40000,
        )


def seed_baseline(db, route, today, daily_averages):
    """Give each prior weekday a single sample equal to that day's average."""
    for offset, average in enumerate(daily_averages, start=1):
        add_day(db, route, today - timedelta(days=offset), [average])


def test_directions_url_is_a_universal_maps_link():
    url = directions_url("1600 Amphitheatre Pkwy", "1 Ferry Building, SF")
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert parsed.netloc == "www.google.com"
    assert parsed.path == "/maps/dir/"
    assert params["api"] == ["1"]
    assert params["origin"] == ["1600 Amphitheatre Pkwy"]
    assert params["destination"] == ["1 Ferry Building, SF"]
    assert params["travelmode"] == ["driving"]


@pytest.mark.parametrize(
    ("z_score", "expected"),
    [
        (2.0, "red"),
        (1.01, "red"),
        (0.5, "yellow"),
        (-0.5, "yellow"),
        (-1.5, "green"),
        (None, "grey"),
    ],
)
def test_classify_maps_z_scores_to_traffic_lights(z_score, expected):
    assert classify(z_score) == expected


def test_a_slow_day_against_a_steady_baseline_is_red(db, route):
    today = date(2026, 2, 10)
    seed_baseline(db, route, today, [1800, 1830, 1770, 1810, 1790, 1820])
    add_day(db, route, today, [2400])

    report = build_report(db, route, today=today)
    assert report.severity == "red"
    assert report.z_score > 1
    assert report.delta_seconds > 0
    assert "slower" in report.title()


def test_a_fast_day_is_green(db, route):
    today = date(2026, 2, 10)
    seed_baseline(db, route, today, [1800, 1830, 1770, 1810, 1790, 1820])
    add_day(db, route, today, [1400])

    report = build_report(db, route, today=today)
    assert report.severity == "green"
    assert "faster" in report.title()


def test_an_ordinary_day_is_yellow(db, route):
    today = date(2026, 2, 10)
    seed_baseline(db, route, today, [1800, 1900, 1700, 1850, 1750, 1800])
    add_day(db, route, today, [1810])

    report = build_report(db, route, today=today)
    assert report.severity == "yellow"
    assert report.label == "A typical day"


def test_a_noisy_route_needs_a_bigger_slip_to_go_red(db, route):
    """Same average, same day, wider spread: 40 minutes is no longer an alarm.

    The steady-baseline test above calls the identical 2400s day red; here the
    route swings by ten minutes either way as a matter of course, so it does not.
    """
    today = date(2026, 2, 10)
    seed_baseline(db, route, today, [1100, 2500, 1400, 2200, 1250, 2350])
    add_day(db, route, today, [2400])

    report = build_report(db, route, today=today)
    assert report.baseline_avg_seconds == 1800  # the same mean as the steady route
    assert report.severity == "yellow"


def test_a_short_history_reports_grey_rather_than_guessing(db, route):
    today = date(2026, 2, 10)
    seed_baseline(db, route, today, [1800, 1830])
    add_day(db, route, today, [3600])

    report = build_report(db, route, today=today)
    # Today has data, but nothing trustworthy to judge it against, so the report
    # says so rather than guessing at a severity.
    assert report.severity == "grey"
    assert report.z_score is None
    assert report.baseline_avg_seconds is None
    assert "building a baseline" in report.body()


def test_no_samples_today_is_grey_and_says_so(db, route):
    today = date(2026, 2, 10)
    seed_baseline(db, route, today, [1800, 1830, 1770, 1810, 1790, 1820])

    report = build_report(db, route, today=today)
    assert report.severity == "grey"
    assert report.has_data is False
    assert "no samples today" in report.title()
    assert "No commute samples" in report.body()


def test_baseline_excludes_today(db, route):
    today = date(2026, 2, 10)
    seed_baseline(db, route, today, [1800, 1800, 1800, 1800, 1800, 1800])
    add_day(db, route, today, [3600])

    report = build_report(db, route, today=today)
    assert report.baseline_avg_seconds == 1800
    assert report.baseline_days == 6
    assert report.avg_seconds == 3600


def test_report_body_lists_today_normal_and_the_delta(db, route):
    today = date(2026, 2, 10)
    seed_baseline(db, route, today, [1800, 1830, 1770, 1810, 1790, 1820])
    add_day(db, route, today, [2100, 2400])

    body = build_report(db, route, today=today).body()
    assert "Today" in body
    assert "Normal" in body
    assert "vs normal" in body


def test_as_dict_carries_the_colour_and_the_maps_link(db, route):
    today = date(2026, 2, 10)
    seed_baseline(db, route, today, [1800, 1830, 1770, 1810, 1790, 1820])
    add_day(db, route, today, [2400])

    payload = build_report(db, route, today=today).as_dict()
    assert payload["color"] == "#d03b3b"
    assert payload["severity"] == "red"
    assert payload["maps_url"].startswith("https://www.google.com/maps/dir/")
    assert payload["label"] == "Busier than normal"
