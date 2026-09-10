import pytest

from commute_tracker.maps import MapsError, build_request, parse_response


def test_parse_response_reads_duration_distance_and_delay():
    travel = parse_response(
        {
            "routes": [
                {"duration": "1834s", "staticDuration": "1500s", "distanceMeters": 42100}
            ]
        }
    )
    assert travel.duration_seconds == 1834
    assert travel.static_duration_seconds == 1500
    assert travel.distance_meters == 42100
    assert travel.delay_seconds == 334


def test_parse_response_rounds_fractional_seconds():
    assert parse_response({"routes": [{"duration": "1834.6s"}]}).duration_seconds == 1835


def test_parse_response_tolerates_missing_optional_fields():
    travel = parse_response({"routes": [{"duration": "600s"}]})
    assert travel.static_duration_seconds is None
    assert travel.distance_meters is None
    assert travel.delay_seconds is None


def test_parse_response_never_reports_negative_delay():
    travel = parse_response({"routes": [{"duration": "600s", "staticDuration": "700s"}]})
    assert travel.delay_seconds == 0


@pytest.mark.parametrize("payload", [{}, {"routes": []}, {"routes": [{"distanceMeters": 10}]}])
def test_parse_response_rejects_unusable_payloads(payload):
    with pytest.raises(MapsError):
        parse_response(payload)


def test_build_request_asks_for_a_traffic_aware_drive():
    body = build_request("A St", "B Ave")
    assert body["origin"] == {"address": "A St"}
    assert body["destination"] == {"address": "B Ave"}
    assert body["travelMode"] == "DRIVE"
    assert body["routingPreference"] == "TRAFFIC_AWARE"
    # Leaving departureTime out means "now", which is what a live probe wants.
    assert "departureTime" not in body
