from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from commute_tracker.config import Route
from commute_tracker.db import Database
from commute_tracker.report import build_report, classify, directions_url, fit_curve

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
            local_dt=datetime(day.year, day.month, day.day, 7, tzinfo=TZ)
            + timedelta(minutes=15 * index),
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


# A window that climbs from 20 to 40 minutes, sampled every 15 minutes from 07:00:
# every day averages 30 minutes, but its first samples are always the fastest.
CLIMB = [1200, 1500, 1800, 2100, 2400]


def seed_climbing_baseline(db, route, today, days=6):
    for offset in range(1, days + 1):
        wobble = 20 * (offset % 3 - 1)
        add_day(db, route, today - timedelta(days=offset), [s + wobble for s in CLIMB])


def test_an_early_morning_running_above_the_usual_curve_is_red(db, route):
    """The morning is still cheap in absolute terms, but dearer than usual for the hour.

    Averaged as-is, the first two samples (27.5 min) sit well under a 30 minute
    baseline and would read green. Against what 07:00 and 07:15 usually cost,
    they run about 22% heavy, and the rest of the curve is projected as such.
    """
    today = date(2026, 2, 10)
    seed_climbing_baseline(db, route, today)
    add_day(db, route, today, [1500, 1800])

    report = build_report(db, route, today=today)
    scale = (1200 * 1500 + 1500 * 1800) / (1200**2 + 1500**2)
    assert report.avg_seconds == 1650
    assert report.in_progress
    assert report.curve_scale == pytest.approx(scale)
    expected = (1500 + 1800 + scale * (1800 + 2100 + 2400)) / 5
    assert report.projected_avg_seconds == pytest.approx(expected, abs=1)
    assert report.severity == "red"
    assert report.title().startswith("\U0001f534 Morning commute: on track for 37 min")
    assert "On track for 36.6 min" in report.body()
    assert "22% above the usual curve" in report.body()


def test_an_early_morning_on_the_usual_curve_is_yellow(db, route):
    today = date(2026, 2, 10)
    seed_climbing_baseline(db, route, today)
    add_day(db, route, today, [1200, 1500])

    report = build_report(db, route, today=today)
    assert report.projected_avg_seconds == pytest.approx(1800, abs=10)
    assert report.severity == "yellow"


def test_a_day_climbing_faster_than_usual_keeps_pulling_away(db, route):
    """1.2x the usual curve: the gap is 4 minutes at 07:00 but 8 by 08:00.

    Scaling the curve, rather than shifting it, is what carries the slope
    forward -- a day climbing more steeply than normal keeps doing so.
    """
    today = date(2026, 2, 10)
    seed_climbing_baseline(db, route, today)
    add_day(db, route, today, [1440, 1800, 2160])

    report = build_report(db, route, today=today)
    assert report.curve_scale == pytest.approx(1.2)
    points = report.projection
    assert [p["measured"] for p in points] == [True, True, True, False, False]
    assert points[3]["seconds"] == pytest.approx(2520)
    assert points[4]["seconds"] == pytest.approx(2880)


def test_a_finished_day_is_scored_on_what_it_actually_measured(db, route):
    today = date(2026, 2, 10)
    seed_climbing_baseline(db, route, today)
    add_day(db, route, today, [1300, 1600, 1900, 2200, 2500])

    report = build_report(db, route, today=today)
    assert not report.in_progress
    assert report.projected_avg_seconds == report.avg_seconds == 1900
    assert "on track" not in report.title()
    assert "On track" not in report.body()


def test_old_sample_times_outside_the_window_are_not_projected(db, route):
    today = date(2026, 2, 10)
    seed_climbing_baseline(db, route, today)
    # A time the route no longer samples (the window ends at 09:00).
    for offset in range(1, 7):
        day = today - timedelta(days=offset)
        db.record_sample(
            route_id=route.id,
            route_name=route.name,
            origin=route.origin,
            destination=route.destination,
            local_dt=datetime(day.year, day.month, day.day, 9, 30, tzinfo=TZ),
            duration_seconds=5000,
            distance_meters=40000,
        )
    add_day(db, route, today, [1200])

    report = build_report(db, route, today=today)
    assert "09:30" not in [p["local_time"] for p in report.projection]


def test_the_curve_smooths_noise_out_of_the_usual_profile():
    """A clean quartic comes back exactly; one jittery clock time is pulled into line."""
    keys = [f"07:{m:02d}" for m in range(0, 60, 6)]

    def quartic(i):
        x = i / 9 * 2 - 1
        return 1800 + 300 * x - 400 * x**2 + 50 * x**3 + 200 * x**4

    clean = {key: quartic(i) for i, key in enumerate(keys)}
    for key, value in fit_curve(clean).items():
        assert value == pytest.approx(clean[key], abs=1e-6)

    noisy = dict(clean)
    noisy["07:24"] += 300
    assert abs(fit_curve(noisy)["07:24"] - clean["07:24"]) < 300


def test_a_short_profile_drops_the_curve_degree_and_passes_through_every_point():
    profile = {"07:00": 1200.0, "07:30": 1800.0, "08:00": 1500.0}
    assert fit_curve(profile) == pytest.approx(profile)
