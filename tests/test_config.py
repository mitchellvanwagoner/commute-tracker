from datetime import time

import pytest

from commute_tracker.config import ConfigError, Route, parse_days, parse_time


def make_route(**overrides) -> Route:
    data = {
        "name": "Morning commute",
        "origin": "A St",
        "destination": "B Ave",
        "window_start": "07:00",
        "window_end": "09:00",
        "interval_minutes": 30,
        "days": "mon,tue",
        "timezone": "UTC",
    }
    data.update(overrides)
    return Route.from_dict(data)


def test_parse_time_accepts_24h():
    assert parse_time("07:05") == time(7, 5)
    assert parse_time("23:59") == time(23, 59)


@pytest.mark.parametrize("value", ["7am", "25:00", "07:60", "", "0700"])
def test_parse_time_rejects_junk(value):
    with pytest.raises(ConfigError):
        parse_time(value)


def test_parse_days_normalizes_and_orders():
    assert parse_days("Friday, monday, MON") == ["mon", "fri"]
    assert parse_days(["tue", "sat"]) == ["tue", "sat"]


def test_parse_days_rejects_unknown():
    with pytest.raises(ConfigError):
        parse_days("mon,funday")


def test_sample_times_span_the_window_inclusively():
    route = make_route(window_start="07:00", window_end="08:00", interval_minutes=15)
    assert route.sample_times() == [time(7, 0), time(7, 15), time(7, 30), time(7, 45), time(8, 0)]


def test_sample_times_stop_inside_the_window_when_interval_does_not_divide_it():
    route = make_route(window_start="07:00", window_end="07:50", interval_minutes=20)
    assert route.sample_times() == [time(7, 0), time(7, 20), time(7, 40)]


def test_route_id_is_slugified_from_the_name():
    assert make_route(name="Evening Commute!").id == "evening-commute"


def test_window_must_move_forward():
    with pytest.raises(ConfigError):
        make_route(window_start="09:00", window_end="07:00")


def test_missing_address_is_rejected():
    with pytest.raises(ConfigError):
        make_route(origin="")


def test_unknown_timezone_is_rejected():
    with pytest.raises(ConfigError):
        make_route(timezone="Mars/Olympus_Mons")
