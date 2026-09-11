"""The monthly Routes API call budget.

Every lookup this app makes is a billed Routes API call. Because the request
sets ``routingPreference: TRAFFIC_AWARE``, Google bills it under the "Compute
Routes Pro" SKU, which carries a free allowance of 5,000 calls per month and
charges for every call past it.

So the meter is not decoration: it is what stands between a mistyped interval
and a surprise invoice. Two things are built on it --

* a **hard stop**: once the month's allowance is gone, no further call is made
  at all, and the reason is recorded where the dashboard will show it;
* a **projection**: before a route is saved, what the schedule would cost over
  a full month is compared against what is left.

Google's billing month runs on Pacific time, so that -- not the route's
timezone, and not UTC -- is what decides which month a call lands in.
"""

from __future__ import annotations

import logging
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# Google's free monthly allowance for the Compute Routes Pro SKU.
FREE_TIER_CALLS = 5000
# ...and for the Autocomplete Requests SKU, which is metered separately.
FREE_TIER_AUTOCOMPLETE = 10000
# Google Cloud closes its billing month on Pacific time.
BILLING_TIMEZONE = "America/Los_Angeles"

# Which recorded call kinds count against which allowance.
ROUTES_KINDS = ("sample", "validate")
PLACES_KINDS = ("autocomplete",)


@dataclass(frozen=True)
class Projection:
    """What a given schedule would cost over a month, against what is left."""

    calls_per_month: int
    used: int
    limit: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def over_by(self) -> int:
        """Calls past the allowance a full month of this schedule would run up."""
        return max(0, self.calls_per_month - self.limit)

    @property
    def exceeds(self) -> bool:
        return self.limit > 0 and self.calls_per_month > self.limit


class CallBudget:
    """Counts billed Routes API calls for the current month and caps them.

    A ``limit`` of 0 (or less) means unlimited: the meter still counts, so usage
    stays visible, but nothing is ever blocked.
    """

    def __init__(
        self,
        db,
        *,
        limit: int = FREE_TIER_CALLS,
        timezone: str = BILLING_TIMEZONE,
        kinds: tuple[str, ...] = ROUTES_KINDS,
        label: str = "Routes API",
    ):
        self.db = db
        self.limit = limit
        self.timezone = timezone
        # Only these kinds count against this allowance; another SKU has its own.
        self.kinds = kinds
        self.label = label

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        return datetime.now(self.tzinfo)

    def billing_month(self) -> str:
        """The current billing period as ``YYYY-MM``."""
        return self.now().strftime("%Y-%m")

    # ----------------------------------------------------------------- counts

    def used(self) -> int:
        return self.db.count_api_calls(self.billing_month(), self.kinds)

    def remaining(self) -> int:
        if self.limit <= 0:
            return -1  # unlimited
        return max(0, self.limit - self.used())

    def exhausted(self) -> bool:
        """True once the month's allowance is spent, so no call should be made."""
        return self.limit > 0 and self.used() >= self.limit

    def record(self, *, kind: str, route_id: str | None = None, ok: bool = True) -> None:
        self.db.record_api_call(
            billing_month=self.billing_month(), kind=kind, route_id=route_id, ok=ok
        )

    # ------------------------------------------------------------ projection

    def project(self, calls_per_month: int) -> Projection:
        return Projection(calls_per_month=calls_per_month, used=self.used(), limit=self.limit)

    def days_left_in_month(self) -> int:
        """Whole days remaining, today included -- how much of the month is still to pay for."""
        now = self.now()
        return monthrange(now.year, now.month)[1] - now.day + 1

    def snapshot(self, calls_per_month: int = 0) -> dict:
        """Everything the dashboard needs to show the meter, in one read."""
        now = self.now()
        days_in_month = monthrange(now.year, now.month)[1]
        used = self.used()
        projection = Projection(calls_per_month=calls_per_month, used=used, limit=self.limit)
        # What the rest of this month costs at the current schedule, added to
        # what is already spent: the honest answer to "will I go over?".
        rest_of_month = round(calls_per_month * self.days_left_in_month() / days_in_month)
        return {
            "billing_month": self.billing_month(),
            "used": used,
            "limit": self.limit,
            "remaining": projection.remaining,
            "unlimited": self.limit <= 0,
            "exhausted": self.exhausted(),
            "label": self.label,
            "by_kind": self.db.api_calls_by_kind(self.billing_month(), self.kinds),
            "projected_calls_per_month": calls_per_month,
            "projected_month_end": used + rest_of_month,
            "projected_over_by": max(0, used + rest_of_month - self.limit)
            if self.limit > 0
            else 0,
            "days_left_in_month": self.days_left_in_month(),
        }
