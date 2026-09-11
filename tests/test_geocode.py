"""Address suggestions: formatting, provider selection, and graceful failure."""

from __future__ import annotations

import httpx
import pytest

from commute_tracker.geocode import AddressSuggester, GeocodeError, _photon_label


def suggester(provider: str, handler) -> AddressSuggester:
    s = AddressSuggester(provider, api_key="k")
    s._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return s


# ------------------------------------------------------------- label building


def test_label_orders_components_into_one_address_line():
    assert _photon_label(
        {
            "housenumber": "1600",
            "street": "Amphitheatre Parkway",
            "city": "Mountain View",
            "state": "CA",
            "postcode": "94043",
            "country": "United States",
        }
    ) == "1600 Amphitheatre Parkway, Mountain View, CA, 94043, United States"


def test_a_named_place_leads_but_keeps_its_street():
    label = _photon_label(
        {"name": "Googleplex", "housenumber": "1600", "street": "Amphitheatre Parkway"}
    )
    assert label == "Googleplex, 1600 Amphitheatre Parkway"


def test_a_name_that_merely_repeats_the_street_is_not_doubled():
    assert _photon_label({"name": "Main Street", "street": "Main Street"}) == "Main Street"


def test_missing_components_are_skipped_not_padded():
    assert _photon_label({"city": "Provo", "country": "United States"}) == "Provo, United States"


# ------------------------------------------------------------------ lookups


async def test_photon_needs_no_key_and_returns_labels():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "photon.komoot.io" in str(request.url)
        # No API key should ever be attached to a free public service.
        assert "X-Goog-Api-Key" not in request.headers
        return httpx.Response(
            200, json={"features": [{"properties": {"name": "Temple Square", "city": "SLC"}}]}
        )

    s = suggester("photon", handler)
    assert await s.suggest("temple square") == ["Temple Square, SLC"]
    await s.aclose()


async def test_short_queries_never_reach_the_provider():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"features": []})

    s = suggester("photon", handler)
    assert await s.suggest("ab") == []
    assert await s.suggest("  ") == []
    assert calls == []
    await s.aclose()


async def test_google_provider_sends_the_key_and_reads_predictions():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Goog-Api-Key"] == "k"
        return httpx.Response(
            200, json={"suggestions": [{"placePrediction": {"text": {"text": "350 5th Ave"}}}]}
        )

    s = suggester("google", handler)
    assert await s.suggest("350 5th") == ["350 5th Ave"]
    await s.aclose()


async def test_provider_failure_raises_rather_than_returning_junk():
    s = suggester("photon", lambda r: httpx.Response(503, text="down"))
    with pytest.raises(GeocodeError):
        await s.suggest("anywhere")
    await s.aclose()


def test_off_disables_lookups_entirely():
    s = AddressSuggester("off")
    assert s.enabled is False
    assert s.is_billable is False


def test_only_google_is_treated_as_billable():
    assert AddressSuggester("photon").is_billable is False
    assert AddressSuggester("google").is_billable is True
