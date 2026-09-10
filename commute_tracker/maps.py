"""Google Maps Routes API client.

Uses the Routes API (``directions/v2:computeRoutes``) rather than the legacy
Distance Matrix API: it is the current, supported endpoint and returns both the
live traffic duration and the free-flow ("static") duration in one call.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELD_MASK = "routes.duration,routes.staticDuration,routes.distanceMeters"
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)s$")


class MapsError(RuntimeError):
    """A lookup against the Routes API failed."""


@dataclass(frozen=True)
class TravelTime:
    """One driving-time measurement."""

    duration_seconds: int
    static_duration_seconds: int | None
    distance_meters: int | None

    @property
    def delay_seconds(self) -> int | None:
        """Seconds of traffic delay versus free-flow driving, when known."""
        if self.static_duration_seconds is None:
            return None
        return max(0, self.duration_seconds - self.static_duration_seconds)


def _parse_duration(value: str | None) -> int | None:
    """Convert a protobuf duration string (``'1234s'``) to whole seconds."""
    if not value:
        return None
    match = _DURATION_RE.match(str(value))
    if not match:
        log.warning("Unrecognized duration from Routes API: %r", value)
        return None
    return round(float(match.group(1)))


def parse_response(payload: dict) -> TravelTime:
    """Turn a computeRoutes response body into a :class:`TravelTime`."""
    routes = payload.get("routes") or []
    if not routes:
        raise MapsError("Routes API returned no route between those addresses")
    route = routes[0]
    duration = _parse_duration(route.get("duration"))
    if duration is None:
        raise MapsError(f"Routes API response had no usable duration: {route!r}")
    distance = route.get("distanceMeters")
    return TravelTime(
        duration_seconds=duration,
        static_duration_seconds=_parse_duration(route.get("staticDuration")),
        distance_meters=int(distance) if distance is not None else None,
    )


def build_request(origin: str, destination: str, traffic_model: str = "TRAFFIC_AWARE") -> dict:
    """Build the computeRoutes request body for a driving trip departing now."""
    return {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": "DRIVE",
        # Omitting departureTime means "now", which is what a live commute probe wants.
        "routingPreference": traffic_model,
        "computeAlternativeRoutes": False,
        "units": "METRIC",
    }


class RoutesClient:
    """Thin async wrapper around the Routes API."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 20.0,
        traffic_model: str = "TRAFFIC_AWARE",
    ):
        self._api_key = api_key
        self._traffic_model = traffic_model
        self._client = httpx.AsyncClient(timeout=timeout)

    async def travel_time(self, origin: str, destination: str) -> TravelTime:
        """Look up the current driving time between two addresses."""
        try:
            response = await self._client.post(
                ROUTES_URL,
                json=build_request(origin, destination, self._traffic_model),
                headers={
                    "X-Goog-Api-Key": self._api_key,
                    "X-Goog-FieldMask": FIELD_MASK,
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise MapsError(f"Could not reach the Routes API: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            detail = response.text.strip()[:500]
            raise MapsError(f"Routes API returned HTTP {response.status_code}: {detail}")

        return parse_response(response.json())

    async def aclose(self) -> None:
        await self._client.aclose()
