# Commute Tracker

Track how long a drive actually takes, day after day.

Give it two addresses and a window of the day (say 07:00–09:00, weekdays). It asks
the Google Maps Routes API for the live driving time every N minutes inside that
window, stores every measurement in SQLite, and serves a dashboard that charts the
minimum, maximum and average for as long as it has been running.

Runs as a single Docker container: the scheduler and the dashboard live in the same
process, and the database sits on a volume so history survives rebuilds.

## What the dashboard shows

- **Headline stats** — average, fastest, slowest, median, 90th percentile, spread.
- **Daily commute range** — a band of each day's fastest-to-slowest trip with the
  daily average drawn through it, so a bad week is obvious at a glance.
- **Time-of-day profile** — every sample time in the window aggregated across all
  days. This is the chart that answers "what time should I leave?"
- **Average by weekday** — which day of the week costs you the most.
- **Daily table** and a **CSV export** of every raw sample.

Charts are hand-rolled SVG with a hover crosshair and tooltips — no CDN, so the
dashboard works on a network with no outbound access.

## Setup

### 1. Get a Google Maps API key

1. Create (or open) a project in the [Google Cloud console](https://console.cloud.google.com/).
2. Enable the **Routes API**. (Not the legacy Distance Matrix API — this project
   uses `directions/v2:computeRoutes`.)
3. Create an **API key** under *APIs & Services → Credentials*, and restrict it to
   the Routes API.

Google's Routes API has a monthly free allowance, and pricing beyond it is per
request. `python -m commute_tracker schedule` prints the estimated call count for
your configuration before you commit to it — a 2-hour window sampled every 15
minutes on weekdays is roughly 200 calls a month.

### 2. Configure

```bash
cp .env.example .env
# then edit .env: API key, the two addresses, the window, and the timezone
```

| Variable | Meaning |
| --- | --- |
| `GOOGLE_MAPS_API_KEY` | Key with the Routes API enabled (required) |
| `ORIGIN_ADDRESS` / `DESTINATION_ADDRESS` | The two addresses, as you would type them into Maps |
| `WINDOW_START` / `WINDOW_END` | The daily tracking window, `HH:MM` 24-hour |
| `SAMPLE_INTERVAL_MINUTES` | How often to sample inside the window |
| `DAYS` | Which days to track, e.g. `mon,tue,wed,thu,fri` |
| `TZ` | Timezone the window is expressed in, e.g. `America/Los_Angeles` |
| `TRAFFIC_MODEL` | `TRAFFIC_AWARE` (default) or `TRAFFIC_AWARE_OPTIMAL` (more accurate, costs more) |
| `PORT` | Dashboard port, default `8080` |
| `DB_PATH` | SQLite file, default `/data/commutes.db` in the container |

### 3. Run

```bash
docker compose up -d --build
```

Then open <http://localhost:8080>.

Samples begin at the next scheduled time inside your window. To confirm the key
works right away without waiting:

```bash
docker compose exec commute-tracker python -m commute_tracker sample
```

## Tracking more than one commute

Copy `routes.example.yml` to `routes.yml`, list as many routes as you like (a
morning and an evening leg, for instance), and uncomment the `routes.yml` bind
mount in `docker-compose.yml`. When `routes.yml` is present it replaces the single
route from `.env`; the API key still comes from the environment. The dashboard
grows a route selector.

## Command line

```bash
python -m commute_tracker serve      # dashboard + scheduler (what the container runs)
python -m commute_tracker sample     # take one measurement now and store it
python -m commute_tracker stats      # print a summary per route
python -m commute_tracker schedule   # show sample times and the monthly call estimate
```

## Running without Docker

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # fill it in; set DB_PATH=data/commutes.db
.venv/bin/python -m commute_tracker serve
```

## How it works

| File | Role |
| --- | --- |
| `commute_tracker/config.py` | Loads routes from `.env` or `routes.yml`; validates windows, days, timezones |
| `commute_tracker/maps.py` | Routes API client; parses live and free-flow durations |
| `commute_tracker/db.py` | SQLite schema, writes, and every aggregation the dashboard needs |
| `commute_tracker/tracker.py` | Turns a window into one cron job per sample time and runs them |
| `commute_tracker/web/app.py` | FastAPI: the JSON API, the CSV export, and the page |
| `commute_tracker/web/static/` | The dashboard — plain HTML, CSS and SVG charts |

Each route's window becomes a set of cron jobs — one per clock time — rather than a
single interval timer. That way every day's samples land at the same clock times,
which is what makes the time-of-day chart comparable across days. Failed lookups
are recorded too, so a gap in a chart can always be explained.

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
ruff check .
```

## License

MIT — see [LICENSE](LICENSE).
