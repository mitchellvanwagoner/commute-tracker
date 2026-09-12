"""The monthly call budget: counting, the hard stop, and the overage warning."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from commute_tracker.config import Route, Settings
from commute_tracker.db import Database
from commute_tracker.maps import BudgetExceededError, MapsError, RoutesClient
from commute_tracker.routes import RouteStore
from commute_tracker.tracker import CommuteTracker
from commute_tracker.usage import BILLING_TIMEZONE, CallBudget

ROUTE_SPEC = {
    "name": "Morning commute",
    "origin": "A St",
    "destination": "B Ave",
    "window_start": "07:00",
    "window_end": "09:00",
    "interval_minutes": 15,
    "days": "mon,tue,wed,thu,fri",
    "timezone": "UTC",
}


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(tmp_path / "usage.db")


def make_tracker(tmp_path, *, limit: int, spec: dict | None = None) -> CommuteTracker:
    routes_file = tmp_path / "routes.yml"
    RouteStore(routes_file).create(spec or ROUTE_SPEC)
    settings = Settings(
        api_key="test-key",
        db_path=tmp_path / "usage.db",
        routes_file=routes_file,
        free_tier_calls=limit,
    )
    return CommuteTracker(settings, notifiers=[])


# ----------------------------------------------------------------- counting


def test_calls_are_counted_per_billing_month(db):
    budget = CallBudget(db, limit=100)
    for _ in range(3):
        budget.record(kind="sample", route_id="r1")
    assert budget.used() == 3
    assert budget.remaining() == 97
    assert not budget.exhausted()


def test_counts_are_scoped_to_the_month(db):
    budget = CallBudget(db, limit=100)
    budget.record(kind="sample")
    # A call booked against a different month must not count against this one.
    db.record_api_call(billing_month="1999-01", kind="sample")
    assert budget.used() == 1


def test_kind_breakdown_separates_tests_from_sampling(db):
    budget = CallBudget(db, limit=100)
    budget.record(kind="sample")
    budget.record(kind="sample")
    budget.record(kind="validate")
    assert db.api_calls_by_kind(budget.billing_month()) == {"sample": 2, "validate": 1}


def test_a_zero_limit_means_unlimited(db):
    budget = CallBudget(db, limit=0)
    for _ in range(50):
        budget.record(kind="sample")
    assert not budget.exhausted()
    assert budget.snapshot()["unlimited"] is True


# ---------------------------------------------------------------- hard stop


async def test_client_refuses_to_call_once_the_budget_is_spent(db):
    budget = CallBudget(db, limit=2)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"routes": [{"duration": "600s"}]})

    client = RoutesClient("key", budget=budget)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await client.travel_time("A", "B")
    await client.travel_time("A", "B")
    assert budget.used() == 2

    with pytest.raises(BudgetExceededError):
        await client.travel_time("A", "B")

    # The third attempt must not have reached Google at all.
    assert len(calls) == 2
    await client.aclose()


async def test_a_network_error_does_not_burn_budget(db):
    budget = CallBudget(db, limit=5)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = RoutesClient("key", budget=budget)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(MapsError):
        await client.travel_time("A", "B")
    assert budget.used() == 0
    await client.aclose()


async def test_an_http_error_still_counts(db):
    """It reached Google, so assume it was billed rather than under-report."""
    budget = CallBudget(db, limit=5)
    client = RoutesClient("key", budget=budget)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, text="bad address"))
    )
    with pytest.raises(MapsError):
        await client.travel_time("A", "B")
    assert budget.used() == 1
    await client.aclose()


async def test_a_blocked_sample_is_recorded_as_an_explained_gap(tmp_path):
    tracker = make_tracker(tmp_path, limit=1)
    route = tracker.routes[0]
    tracker.budget.record(kind="sample")  # spend the allowance

    assert await tracker.sample_route(route) is None
    failures = tracker.db.recent_failures(route.id)
    assert len(failures) == 1
    assert "budget" in failures[0]["message"].lower()
    await tracker.shutdown()


# ------------------------------------------------------------ overage warning


def test_no_warning_when_the_schedule_fits(tmp_path):
    tracker = make_tracker(tmp_path, limit=5000)
    # 9 samples/day x 5 days x 52/12 = 195 calls a month.
    assert tracker.estimated_calls_per_month() == 195
    assert tracker.overage_warning() is None


def test_warns_when_the_schedule_would_run_past_the_allowance(tmp_path):
    tracker = make_tracker(tmp_path, limit=100)
    warning = tracker.overage_warning()
    assert warning is not None
    assert "over the 100 free-tier allowance" in warning


def test_a_pending_edit_replaces_its_stored_self_rather_than_doubling(tmp_path):
    """Costing an edit must not count the route twice."""
    tracker = make_tracker(tmp_path, limit=5000)
    stored = tracker.routes[0]
    pending = Route.from_dict({**ROUTE_SPEC, "id": stored.id, "interval_minutes": 15})
    # Same schedule as stored, so the projection must be unchanged, not doubled.
    routes = [r for r in tracker.active_routes() if r.id != pending.id] + [pending]
    assert tracker.estimated_calls_per_month(routes) == 195


def test_a_disabled_pending_route_costs_nothing(tmp_path):
    tracker = make_tracker(tmp_path, limit=5000)
    stored = tracker.routes[0]
    paused = Route.from_dict({**ROUTE_SPEC, "id": stored.id, "enabled": False})
    assert tracker.overage_warning(paused) is None


def test_warns_about_a_sustained_overage_even_when_this_month_squeaks_by(tmp_path):
    """A schedule that only fits because the month is nearly over still gets flagged.

    Staying quiet here would just move the bill to next month.
    """
    tracker = make_tracker(tmp_path, limit=150)  # schedule costs 195/month
    snapshot = tracker.budget.snapshot(tracker.estimated_calls_per_month())
    warning = tracker.overage_warning()
    assert warning is not None
    if not snapshot["projected_over_by"]:
        # Late enough in the month that proration clears it: the sustained
        # wording must be what comes back.
        assert "over a full month" in warning


async def test_the_test_button_is_recorded_as_a_test_not_a_sample(tmp_path):
    """The usage breakdown is only useful if each kind is labelled honestly."""
    import httpx
    from fastapi.testclient import TestClient

    from commute_tracker.web.app import create_app

    routes_file = tmp_path / "routes.yml"
    RouteStore(routes_file).create(ROUTE_SPEC)
    settings = Settings(
        api_key="k", db_path=tmp_path / "v.db", routes_file=routes_file, free_tier_calls=100
    )
    app = create_app(settings, run_scheduler=False)
    tracker = app.state.tracker
    tracker.client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"routes": [{"duration": "600s"}]})
        )
    )
    with TestClient(app) as client:
        assert client.post(
            "/api/routes/validate", json={"origin": "A", "destination": "B"}
        ).status_code == 200

    assert tracker.db.api_calls_by_kind(tracker.budget.billing_month()) == {"validate": 1}


def test_month_end_projection_does_not_count_today_twice(tmp_path):
    """`used` already holds today's calls, so today is not still to come.

    Pricing today on both sides adds a day of sampling that has in fact been
    counted, nudging the overage warning earlier than the schedule earns.
    """
    db = Database(tmp_path / "proj.db")
    budget = CallBudget(db, limit=1000)
    # 30 calls a month over a 30-day month is one a day. Stand on the last day
    # of the month with that day's call already spent: nothing is left to come,
    # so the projection is exactly what the meter reads.
    budget.now = lambda: datetime(2026, 4, 30, 18, 0, tzinfo=ZoneInfo(BILLING_TIMEZONE))
    budget.record(kind="sample")
    snapshot = budget.snapshot(calls_per_month=30)
    assert snapshot["used"] == 1
    assert snapshot["days_left_in_month"] == 1
    assert snapshot["projected_month_end"] == 1


def test_month_end_projection_prices_the_days_still_to_come(tmp_path):
    db = Database(tmp_path / "proj2.db")
    budget = CallBudget(db, limit=1000)
    # The 1st of a 30-day month: today is spent, 29 days remain to pay for.
    budget.now = lambda: datetime(2026, 4, 1, 18, 0, tzinfo=ZoneInfo(BILLING_TIMEZONE))
    budget.record(kind="sample")
    snapshot = budget.snapshot(calls_per_month=30)
    assert snapshot["projected_month_end"] == 1 + 29
