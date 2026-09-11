"""Address suggestions for the route editor.

Deliberately not Google by default. The addresses this app needs are just
strings handed to the Routes API, and there is no reason to put a paid,
key-gated, per-keystroke API behind a box someone types in a few times a year.

Three providers:

``photon``  (default) Komoot's public OpenStreetMap geocoder. No key, no
            billing, no Cloud Console setup, and built for type-ahead.
``google``  Places API (New) Autocomplete. Costs money past 10,000 requests a
            month and must be enabled separately in Cloud Console -- including
            on the API key's own restrictions, which by default only allow the
            Routes API. Opt in only if Photon's coverage disappoints.
``off``     No suggestions; the address boxes stay plain text.

Whatever a provider returns is a *suggestion*, never a guarantee: the editor's
Test button is what proves an address actually resolves for the Routes API.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

PHOTON_URL = "https://photon.komoot.io/api/"
GOOGLE_AUTOCOMPLETE_URL = "https://places.googleapis.com/v1/places:autocomplete"

# Suggestions are for picking from, not browsing. A short list keeps the
# datalist usable and the request cheap.
MAX_SUGGESTIONS = 6
# Below this, a query is too vague to be worth a request.
MIN_QUERY_LENGTH = 3


class GeocodeError(RuntimeError):
    """A suggestion lookup failed. Never fatal -- the box still takes typing."""


def _photon_label(properties: dict) -> str:
    """Flatten a Photon feature into one address line.

    Photon returns components, not a formatted string, so the order has to be
    imposed here: house number and street first, then the locality, then the
    region -- which is the order these read in for the countries this is most
    likely to be used in, and the order the Routes API parses most reliably.
    """
    street = " ".join(
        part for part in (properties.get("housenumber"), properties.get("street")) if part
    )
    # A named place (a business, a station) is more recognizable than its
    # street address, but only when it is not just the street repeated back.
    name = properties.get("name") or ""
    head = name if name and name != street else street
    if head and street and head != street:
        head = f"{head}, {street}"

    parts = [
        head,
        properties.get("city") or properties.get("county"),
        properties.get("state"),
        properties.get("postcode"),
        properties.get("country"),
    ]
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(str(part))
    return ", ".join(seen)


async def _photon(query: str, client: httpx.AsyncClient) -> list[str]:
    response = await client.get(
        PHOTON_URL,
        params={"q": query, "limit": MAX_SUGGESTIONS},
        # Photon is a free public service; identify the caller as its usage
        # guidance asks rather than turning up anonymously.
        headers={"User-Agent": "commute-tracker (https://github.com/topics/commute-tracker)"},
    )
    if response.status_code != httpx.codes.OK:
        raise GeocodeError(f"Photon returned HTTP {response.status_code}")
    features = response.json().get("features") or []
    labels = [_photon_label(f.get("properties") or {}) for f in features]
    return [label for label in labels if label]


async def _google(query: str, client: httpx.AsyncClient, api_key: str) -> list[str]:
    response = await client.post(
        GOOGLE_AUTOCOMPLETE_URL,
        json={"input": query},
        headers={"X-Goog-Api-Key": api_key, "Content-Type": "application/json"},
    )
    if response.status_code != httpx.codes.OK:
        raise GeocodeError(
            f"Places API returned HTTP {response.status_code}: {response.text[:200]}. "
            "The Places API (New) must be enabled in Cloud Console, and allowed on the "
            "API key's own restrictions."
        )
    suggestions = response.json().get("suggestions") or []
    labels = []
    for item in suggestions[:MAX_SUGGESTIONS]:
        text = ((item.get("placePrediction") or {}).get("text") or {}).get("text")
        if text:
            labels.append(text)
    return labels


class AddressSuggester:
    """Looks up address suggestions from whichever provider is configured."""

    def __init__(self, provider: str = "photon", *, api_key: str = "", timeout: float = 8.0):
        self.provider = (provider or "photon").strip().lower()
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def enabled(self) -> bool:
        return self.provider in {"photon", "google"}

    @property
    def is_billable(self) -> bool:
        """Whether a lookup costs money, which decides if it needs metering."""
        return self.provider == "google"

    def will_request(self, query: str) -> bool:
        """Whether this query would actually go out to the provider.

        The single source of truth for that question, so a caller that meters
        billable lookups cannot disagree with what ``suggest`` really does.
        """
        return self.enabled and len((query or "").strip()) >= MIN_QUERY_LENGTH

    async def suggest(self, query: str) -> list[str]:
        if not self.will_request(query):
            return []
        query = query.strip()
        try:
            if self.provider == "google":
                return await _google(query, self._client, self._api_key)
            return await _photon(query, self._client)
        except httpx.HTTPError as exc:
            raise GeocodeError(f"Could not reach the address provider: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
