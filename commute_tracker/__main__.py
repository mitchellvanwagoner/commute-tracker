"""Command line entry point.

    python -m commute_tracker serve      # dashboard + scheduler (the container default)
    python -m commute_tracker sample     # take one measurement now and store it
    python -m commute_tracker stats      # print the summary for each route
    python -m commute_tracker schedule   # show when samples will be taken
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import ConfigError, load_settings
from .db import Database
from .tracker import CommuteTracker


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)


def _minutes(seconds: float | None) -> str:
    return "--" if seconds is None else f"{seconds / 60:.1f} min"


def cmd_serve(args) -> int:
    import uvicorn

    from .web.app import create_app

    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="debug" if args.verbose else "info",
    )
    return 0


def cmd_sample(args) -> int:
    settings = load_settings()
    tracker = CommuteTracker(settings)

    async def run() -> list[dict]:
        try:
            return await tracker.sample_all()
        finally:
            await tracker.shutdown()

    results = asyncio.run(run())
    for result in results:
        print(f"{result['route_id']}: {_minutes(result['duration_seconds'])}")
    return 0 if results else 1


def cmd_stats(args) -> int:
    settings = load_settings()
    db = Database(settings.db_path)
    for route in settings.routes:
        summary = db.summary(route.id, days=args.days)
        print(f"\n{route.name} ({route.origin} -> {route.destination})")
        if not summary["samples"]:
            print("  no samples yet")
            continue
        print(f"  window     {summary['first_date']} .. {summary['last_date']}")
        print(f"  samples    {summary['samples']} over {summary['days_tracked']} day(s)")
        print(f"  fastest    {_minutes(summary['min_seconds'])}")
        print(f"  average    {_minutes(summary['avg_seconds'])}")
        print(f"  median     {_minutes(summary['median_seconds'])}")
        print(f"  p90        {_minutes(summary['p90_seconds'])}")
        print(f"  slowest    {_minutes(summary['max_seconds'])}")
        if summary["failures"]:
            print(f"  failures   {summary['failures']}")
    return 0


def cmd_schedule(args) -> int:
    settings = load_settings()
    tracker = CommuteTracker(settings)
    for route in settings.routes:
        times = [t.strftime("%H:%M") for t in route.sample_times()]
        print(f"\n{route.name} [{route.id}] ({route.timezone})")
        print(f"  days    {', '.join(route.days)}")
        print(f"  samples {len(times)}/day: {', '.join(times)}")
    print(f"\n~{tracker.estimated_calls_per_month()} Routes API calls per month")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="commute-tracker", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="run the dashboard and the sampling scheduler")
    sub.add_parser("sample", help="take one measurement now")

    stats = sub.add_parser("stats", help="print a summary per route")
    stats.add_argument("--days", type=int, default=None, help="limit to the last N days")

    sub.add_parser("schedule", help="show the planned sample times")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    handlers = {
        "serve": cmd_serve,
        "sample": cmd_sample,
        "stats": cmd_stats,
        "schedule": cmd_schedule,
    }
    handler = handlers.get(args.command or "serve")
    try:
        return handler(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
