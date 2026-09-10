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
- **Today's status** — a colored dot and a plain-English verdict, the same one the
  daily push notification sends.
- **Route management** — add, edit, pause and delete routes from the page itself;
  no restart, no editing files.

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

`.env` only needs the API key and one starter route. **Routes are stored in the
database and edited from the dashboard** — `.env` and `routes.yml` seed them the
first time the app runs against a fresh database, and are not read again. That is
deliberate: a restart must never silently undo an edit you made in the UI.

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
| `NOTIFY_AT` + a service | Daily push notification — see [below](#daily-push-notification) |

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

## Daily push notification

At a time you choose, the tracker pushes that day's commute to your phone: how
long it took, how that compares to normal, a severity color, and a tap-through
Google Maps link that opens the route in the Maps app.

    🔴 Morning commute: 39 min, 8% slower

    Busier than normal
    Today  avg 38.6 min  (best 32.8, worst 43.1) from 9 samples
    Normal 35.8 min ± 1.8 over the last 21 days
    Today is +2.7 min vs normal

### Severity

The day's average is compared against the trailing baseline of *daily* averages,
measured in standard deviations rather than a flat percentage — so a route that
swings by ten minutes either way as a matter of course does not cry wolf, while a
normally metronomic route flags a smaller slip.

| Color | Meaning | Rule |
| --- | --- | --- |
| 🔴 red | Busier than normal | more than +1σ above the baseline |
| 🟡 yellow | A typical day | within ±1σ |
| 🟢 green | Lighter than normal | more than −1σ below the baseline |
| ⚪ grey | Not enough history yet | fewer than `NOTIFY_MIN_BASELINE_DAYS` of data, or no samples today |

Tune with `NOTIFY_THRESHOLD_SIGMA` (default `1.0`), `NOTIFY_BASELINE_DAYS`
(default 30) and `NOTIFY_MIN_BASELINE_DAYS` (default 5). Red notifications are
sent at high priority so they can break through quiet hours; grey ones are sent
quietly.

### Choosing a service

Set up either or both — whichever have credentials get the report, and if neither
does, no notification job is scheduled.

**[ntfy](https://ntfy.sh)** — free, no account. Install the app, subscribe to a
topic name that nobody else would guess (anyone who knows the topic can read it),
and set `NTFY_TOPIC` to the same string. `NTFY_SERVER` and `NTFY_TOKEN` point it
at a self-hosted or protected instance instead.

**[Pushover](https://pushover.net)** — one-time purchase per platform. Create an
application to get its API token, then set `PUSHOVER_TOKEN` and `PUSHOVER_USER`
(your user key, on the Pushover dashboard).

Then pick a time — set `NOTIFY_AT` to something after `WINDOW_END` so the report
covers the whole window:

```bash
NOTIFY_AT=09:30
NTFY_TOPIC=your-hard-to-guess-topic
```

Send one immediately to check it all works:

```bash
docker compose exec commute-tracker python -m commute_tracker notify
```

## Managing routes

The **Routes** card on the dashboard is the normal way to do this. *Add a route*
opens an editor for the two addresses, the tracking window, how often to check,
which days, the timezone and an optional daily report time. **Test addresses**
does a live lookup without saving, so you find out that an address is ambiguous
or that your API key is wrong before you commit to it.

Each route can be:

- **Edited** — including its addresses. The route keeps its identity, so its
  collected history stays attached (renaming it does not orphan the data).
- **Paused** — stops sampling and stops the report, keeps everything recorded.
- **Deleted** — you are asked whether to keep the history. Kept history stays
  visible in the dashboard as a read-only route; re-creating a route with the
  same name reclaims its old id and reattaches the data.

Every change takes effect immediately: the API reschedules that route's jobs as
part of the same request, so nothing needs restarting.

### Seeding several routes at once

To start with more than one route, copy `routes.example.yml` to `routes.yml`,
list them there, and uncomment the `routes.yml` bind mount in
`docker-compose.yml`. Remember this is a *seed*: once the database exists, the
dashboard owns the routes and the file is ignored.

## Command line

```bash
python -m commute_tracker serve      # dashboard + scheduler (what the container runs)
python -m commute_tracker sample     # take one measurement now and store it
python -m commute_tracker stats      # print a summary per route
python -m commute_tracker schedule   # show sample times and the monthly call estimate
python -m commute_tracker report     # print today's report and its severity
python -m commute_tracker notify     # push today's report now
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
| `commute_tracker/report.py` | Scores today against the baseline; severity, wording, Maps link |
| `commute_tracker/notify.py` | Push delivery (ntfy, Pushover); one class per provider |
| `commute_tracker/db.py` | SQLite schema, writes, and every aggregation the dashboard needs |
| `commute_tracker/routes.py` | The route store: validation, CRUD, and one-time seeding from config |
| `commute_tracker/tracker.py` | Turns a window into one cron job per sample time and runs them |
| `commute_tracker/web/app.py` | FastAPI: the JSON API, the CSV export, and the page |
| `commute_tracker/web/static/` | The dashboard — plain HTML, CSS and SVG charts |

### A note on access

The dashboard can change what the tracker does and spends API quota, and it has
no login. That is fine on a home network; do not port-forward it to the open
internet. Put it behind your existing reverse proxy, VPN or Tailscale if you want
it from outside.

Each route's window becomes a set of cron jobs — one per clock time — rather than a
single interval timer. That way every day's samples land at the same clock times,
which is what makes the time-of-day chart comparable across days. Failed lookups
are recorded too, so a gap in a chart can always be explained.

The daily report reads the same aggregations the dashboard does, so the badge on
the page and the notification on your phone can never disagree. A provider that
fails is logged and reported per-channel rather than raised — one dead channel
must not silence the other or take the scheduler down.

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
ruff check .
```

## License

MIT — see [LICENSE](LICENSE).
