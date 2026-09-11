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
# then edit .env: just the API key is enough to start
```

`.env` holds installation-wide settings — the API key, where data lives, the
port, how reports are scored, notifier credentials. **It holds no route data.**

| Variable | Meaning |
| --- | --- |
| `GOOGLE_MAPS_API_KEY` | Key with the Routes API enabled (required) |
| `DB_PATH` | SQLite file, default `/data/commutes.db` in the container |
| `ROUTES_FILE` | Route file, default `routes.yml` beside the database |
| `PORT` | Dashboard port, default `8080` |
| `TZ` | Container clock, and the timezone prefilled when adding a route |
| `TRAFFIC_MODEL` | `TRAFFIC_AWARE` (default) or `TRAFFIC_AWARE_OPTIMAL` (more accurate, costs more) |
| notifier + scoring | See [the notification section](#daily-push-notification) |

### 3. Run

```bash
docker compose up -d --build
```

Open <http://localhost:8080>. A fresh install has **no routes** — the dashboard
comes up empty with an **Add a route** button, which is where you enter the two
addresses and the window you want tracked. **Test addresses** in the editor does
a live lookup without saving, so you can confirm the addresses resolve and the
API key works before committing.

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

Then set the **report time on the route itself**, in the dashboard editor
("Daily report at"). Pick something after the window closes so the report covers
the whole window — the log warns you if it does not. Leave it blank for a route
you do not want notifications about.

Send one immediately to check it all works:

```bash
docker compose exec commute-tracker python -m commute_tracker notify
```

## Managing routes

The **Routes** card on the dashboard is where routes are added, edited, paused
and deleted. Each one has two addresses, a tracking window, how often to check
inside it, which days, a timezone and an optional daily report time.

Every change is **written straight to `routes.yml`**, so routes survive a
restart, a rebuild, or the database being deleted. By default that file sits
beside the database (`/data/routes.yml` in the container), on the same volume,
so one thing to back up and nothing to remember.

A route can be:

- **Edited** — including its addresses. It keeps its id, so its collected
  history stays attached; renaming it does not orphan the data.
- **Paused** — stops sampling and stops the report, keeps everything recorded.
- **Deleted** — you are asked whether to keep the history. Kept history stays
  visible as a read-only route, and re-creating a route with the same name
  reclaims its old id and reattaches the data.

Having **no routes at all** is a normal state, not an error: delete them all and
the dashboard simply comes up empty with the editor ready.

Changes take effect immediately — the API reschedules that route's jobs inside
the same request, so nothing needs restarting.

### Editing routes.yml by hand

The file is yours to read, edit, back up or commit. It looks like
[`routes.example.yml`](routes.example.yml), and a change on disk is picked up
without a restart. Two things to know:

- **Quote the times.** YAML reads a bare `16:30` as a number.
- **Keep each route's `id`.** Samples are recorded against the id, so changing
  one orphans that route's history.

Writes are atomic (written to a temp file, then renamed), so an interrupted
write cannot leave you with a truncated file.

To start from a prepared file, copy it to `/data/routes.yml` before first run:

```bash
docker compose cp routes.yml commute-tracker:/data/routes.yml
docker compose restart commute-tracker
```

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
| `commute_tracker/routes.py` | The route store: reads and writes `routes.yml`, validates every edit |
| `commute_tracker/tracker.py` | Turns a window into one cron job per sample time and runs them |
| `commute_tracker/web/app.py` | FastAPI: the JSON API, the CSV export, and the page |
| `commute_tracker/web/static/` | The dashboard — plain HTML, CSS and SVG charts |

### A note on access

The dashboard can change what the tracker does and spends API quota, and it ships
with no authentication of its own — put it behind whatever you already use
(reverse proxy, VPN, Tailscale) rather than exposing it directly.

Each route's window becomes a set of cron jobs — one per clock time — rather than a
single interval timer. That way every day's samples land at the same clock times,
which is what makes the time-of-day chart comparable across days. Failed lookups
are recorded too, so a gap in a chart can always be explained.

The daily report reads the same aggregations the dashboard does, so the badge on
the page and the notification on your phone can never disagree. A provider that
fails is logged and reported per-channel rather than raised — one dead channel
must not silence the other or take the scheduler down.

## Upgrading from an earlier version

An install whose routes were stored in the database migrates itself: on first
start the routes are written out to `routes.yml` and the old table is dropped.
Nothing is lost and there is nothing to do.

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
ruff check .
```

## License

MIT — see [LICENSE](LICENSE).
