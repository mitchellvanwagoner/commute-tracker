/* Dashboard controller: fetch stats, fill the tiles, draw the three charts. */

const state = { routes: [], routeId: null, days: 30, stats: null };

const $ = (id) => document.getElementById(id);

const minutes = (seconds) => (seconds == null ? null : seconds / 60);
const fmt1 = (value) => (value == null ? "—" : value.toFixed(1));

/** "18.4 min" for a duration in seconds, or an em dash when unknown. */
function durationText(seconds) {
  const value = minutes(seconds);
  return value == null ? "—" : `${value.toFixed(1)} min`;
}

function dayLabel(isoDate) {
  const [y, m, d] = isoDate.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const WEEKDAY_NAMES = {
  mon: "Monday", tue: "Tuesday", wed: "Wednesday", thu: "Thursday",
  fri: "Friday", sat: "Saturday", sun: "Sunday",
};

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) {
    // Prefer the server's own explanation. A malformed routes.yml comes back
    // as a 400 whose detail names the offending line, and "400 Bad Request"
    // on its own would throw that away.
    const detail = await response
      .json()
      .then((body) => body?.detail)
      .catch(() => null);
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

function queryString() {
  const params = new URLSearchParams();
  if (state.routeId) params.set("route", state.routeId);
  if (state.days) params.set("days", String(state.days));
  return params.toString();
}

function renderRouteLine() {
  const route = state.routes.find((r) => r.id === state.routeId);
  if (!route) return;
  // A route that was deleted but kept its history has no window to describe.
  const window = route.configured
    ? ` · ${route.window_start}–${route.window_end} every ${route.interval_minutes} min · ${route.days.join(", ")}`
    : " · no longer tracked";
  $("route-line").textContent = `${route.origin} → ${route.destination}${window}`;
}

function tile(label, value, unit) {
  return `<div class="tile"><p class="label">${label}</p><p class="value">${value}${
    unit ? `<span class="unit">${unit}</span>` : ""
  }</p></div>`;
}

function renderSummary(summary) {
  const avg = minutes(summary.avg_seconds);
  $("hero-value").textContent = avg == null ? "—" : `${avg.toFixed(1)} min`;
  $("hero-sub").textContent = summary.samples
    ? `${summary.samples.toLocaleString()} samples across ${summary.days_tracked} day${
        summary.days_tracked === 1 ? "" : "s"
      } (${summary.first_date} – ${summary.last_date})`
    : "Waiting for the first sample in the tracked window.";

  const spread =
    summary.min_seconds == null
      ? null
      : minutes(summary.max_seconds) - minutes(summary.min_seconds);
  $("tiles").innerHTML = [
    tile("Fastest", fmt1(minutes(summary.min_seconds)), " min"),
    tile("Slowest", fmt1(minutes(summary.max_seconds)), " min"),
    tile("Median", fmt1(minutes(summary.median_seconds)), " min"),
    tile("90th percentile", fmt1(minutes(summary.p90_seconds)), " min"),
    tile("Spread", fmt1(spread), " min"),
    tile(
      "Distance",
      summary.avg_distance_meters == null
        ? "—"
        : (summary.avg_distance_meters / 1000).toFixed(1),
      " km"
    ),
  ].join("");
}

/** Today's status pill: a colored dot plus the wording that explains it. */
function renderReport(report) {
  const pill = $("today-status");
  if (!report) {
    pill.hidden = true;
    return;
  }
  pill.hidden = false;
  $("today-dot").style.background = report.color;
  const parts = [report.label];
  // Mid-window the day is judged on where it is heading, not its average so
  // far -- which, with the fast early trips only, would always read light.
  const dayAvg = report.projected_avg_seconds ?? report.avg_seconds;
  const lead = report.in_progress ? "on track for" : "today";
  if (report.samples && report.delta_percent != null) {
    const direction = report.delta_percent >= 0 ? "slower" : "faster";
    parts.push(
      `${lead} ${(dayAvg / 60).toFixed(1)} min, ` +
        `${Math.abs(report.delta_percent).toFixed(0)}% ${direction} than normal`
    );
  } else if (report.samples) {
    parts.push(`today ${(report.avg_seconds / 60).toFixed(1)} min`);
  } else {
    parts.push("no samples yet today");
  }
  $("today-text").textContent = parts.join(" · ");
  if (report.maps_url) $("maps-link").href = report.maps_url;
}

/**
 * Today's line for the time-of-day chart, one entry per chart row: what each
 * time measured today, then the report's projection for the times to come.
 */
function todayOverlay(rows, stats) {
  const byTime = new Map();
  for (const point of stats.report?.projection ?? []) {
    byTime.set(point.local_time, { value: minutes(point.seconds), measured: point.measured });
  }
  // Measured samples win over the projection's copy, and are drawn even when
  // there is no baseline yet to project from.
  for (const sample of stats.today ?? []) {
    byTime.set(sample.local_time, { value: minutes(sample.duration_seconds), measured: true });
  }
  return rows.map((row) => byTime.get(row.local_time) ?? null);
}

function renderCharts(stats) {
  Charts.bandChart(
    $("chart-daily"),
    stats.daily.map((row) => ({
      label: dayLabel(row.local_date),
      lo: minutes(row.p25_seconds),
      hi: minutes(row.p75_seconds),
      min: minutes(row.min_seconds),
      max: minutes(row.max_seconds),
      avg: minutes(row.avg_seconds),
      samples: row.samples,
      date: row.local_date,
      weekday: row.weekday,
    })),
    {
      tooltip: (row) => `${WEEKDAY_NAMES[row.weekday] ?? ""} ${row.date}`.trim(),
      bandLabel: "Middle half of that day’s trips",
      lineLabel: "Daily average",
      emptyMessage: "No samples yet — the first ones land during the next tracked window.",
    }
  );

  Charts.bandChart(
    $("chart-tod"),
    stats.time_of_day.map((row) => ({
      label: row.local_time,
      lo: minutes(row.p25_seconds),
      hi: minutes(row.p75_seconds),
      min: minutes(row.min_seconds),
      max: minutes(row.max_seconds),
      avg: minutes(row.avg_seconds),
      samples: row.samples,
    })),
    {
      tooltip: (row) => `Departing ${row.label}`,
      bandLabel: "Middle half of days at that time",
      lineLabel: "Average at that time",
      overlay: todayOverlay(stats.time_of_day, stats),
      overlayLabel: "Today",
      projectedLabel: "Today, projected",
      emptyMessage: "No samples yet.",
    }
  );

  Charts.barChart(
    $("chart-weekday"),
    stats.weekday.map((row) => ({
      label: WEEKDAY_NAMES[row.weekday]?.slice(0, 3) ?? row.weekday,
      value: minutes(row.avg_seconds),
      min: minutes(row.min_seconds),
      max: minutes(row.max_seconds),
      samples: row.samples,
      weekday: row.weekday,
    })),
    { tooltip: (row) => WEEKDAY_NAMES[row.weekday] ?? row.weekday }
  );
}

function renderTable(daily) {
  if (!daily.length) {
    $("table-wrap").innerHTML = '<p class="empty">No samples yet.</p>';
    return;
  }
  const rows = daily
    .slice()
    .reverse()
    .map(
      (row) => `<tr>
        <td>${row.local_date} <span style="opacity:.6">${WEEKDAY_NAMES[row.weekday]?.slice(0, 3) ?? ""}</span></td>
        <td>${row.samples}</td>
        <td>${durationText(row.min_seconds)}</td>
        <td>${durationText(row.avg_seconds)}</td>
        <td>${durationText(row.max_seconds)}</td>
      </tr>`
    )
    .join("");
  $("table-wrap").innerHTML = `<div class="table-scroll"><table>
    <thead><tr><th>Date</th><th>Samples</th><th>Fastest</th><th>Average</th><th>Slowest</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function renderFailures(failures) {
  const card = $("failures-card");
  if (!failures.length) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  // Escaped: the message embeds up to 500 characters of a third-party HTTP
  // response body, which has no business being trusted as markup.
  $("failures").innerHTML = failures
    .map(
      (f) =>
        `<p class="failure-row"><span class="when">${escapeHtml(f.local_date)} ${escapeHtml(
          f.local_time
        )}</span>${escapeHtml(f.message)}</p>`
    )
    .join("");
}

/** The month's Routes API meter: how much of the free tier is gone. */
function renderUsage(usage) {
  const card = $("usage-card");
  if (!card) return;
  if (!usage || usage.unlimited) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const pct = usage.limit ? Math.min(100, (usage.used / usage.limit) * 100) : 0;
  const bar = $("usage-bar");
  bar.style.width = `${pct}%`;
  bar.className = usage.exhausted ? "bar stop" : usage.projected_over_by ? "bar warn" : "bar";

  $("usage-text").textContent =
    `${usage.used.toLocaleString()} of ${usage.limit.toLocaleString()} free calls used ` +
    `this month (${usage.billing_month})`;

  const note = $("usage-note");
  if (usage.exhausted) {
    note.textContent =
      "Allowance spent — sampling is paused until next billing month. No further API calls " +
      "will be made.";
    note.className = "usage-note bad";
  } else if (usage.projected_over_by) {
    note.textContent =
      `On the current schedule this month will end at about ` +
      `${usage.projected_month_end.toLocaleString()} calls — ` +
      `${usage.projected_over_by.toLocaleString()} over the free tier.`;
    note.className = "usage-note warn";
  } else {
    note.textContent =
      `Projected ${usage.projected_month_end.toLocaleString()} by month end, ` +
      `within the free tier.`;
    note.className = "usage-note";
  }
}

async function refresh() {
  const [stats, usage] = await Promise.all([
    getJSON(`/api/stats?${queryString()}`),
    getJSON("/api/usage").catch(() => null),
  ]);
  state.stats = stats;
  renderUsage(usage);
  $("csv-link").href = `/api/samples.csv?${queryString()}`;
  renderSummary(stats.summary);
  renderReport(stats.report);
  renderCharts(stats);
  renderTable(stats.daily);
  renderFailures(stats.failures);
}

/** Re-read the routes, keeping the current selection if it still exists. */
async function loadRoutes() {
  state.routes = await getJSON("/api/routes");
  const select = $("route-select");
  const previous = state.routeId;
  select.innerHTML = state.routes
    .map((route) => {
      const suffix = route.enabled === false ? " (paused)" : "";
      return `<option value="${route.id}">${escapeHtml(route.name)}${suffix}</option>`;
    })
    .join("");
  state.routeId = state.routes.some((r) => r.id === previous)
    ? previous
    : (state.routes[0]?.id ?? null);
  select.value = state.routeId ?? "";
  select.disabled = state.routes.length < 2;
  renderRouteLine();
  Routes.renderTable(state.routes);
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = value ?? "";
  return node.innerHTML;
}

/** Blank out the charts and stats when there is no route to show. */
function showEmptyState() {
  getJSON("/api/usage")
    .then(renderUsage)
    .catch(() => {});
  $("route-line").textContent = "No routes yet \u2014 add one below to start tracking a commute.";
  $("hero-value").textContent = "\u2014";
  $("hero-sub").textContent = "";
  $("tiles").innerHTML = "";
  $("today-status").hidden = true;
  $("table-wrap").innerHTML = "";
  $("failures-card").hidden = true;
  for (const id of ["chart-daily", "chart-tod", "chart-weekday"]) {
    $(id).innerHTML = '<p class="empty">Nothing to chart yet.</p>';
  }
  for (const id of ["csv-link", "maps-link"]) {
    $(id).removeAttribute("href");
    $(id).setAttribute("aria-disabled", "true");
  }
}

/** Everything the page shows, re-read from the server. */
async function reload() {
  await loadRoutes();
  if (!state.routeId) {
    showEmptyState();
    return;
  }
  for (const id of ["csv-link", "maps-link"]) $(id).removeAttribute("aria-disabled");
  await refresh();
}

async function init() {
  Routes.init();
  await loadRoutes();

  $("route-select").addEventListener("change", (event) => {
    state.routeId = event.target.value;
    renderRouteLine();
    refresh();
  });
  $("range-select").addEventListener("change", (event) => {
    state.days = event.target.value ? Number(event.target.value) : null;
    refresh();
  });
  $("table-toggle").addEventListener("click", () => {
    const wrap = $("table-wrap");
    wrap.hidden = !wrap.hidden;
    $("table-toggle").textContent = wrap.hidden ? "Show table" : "Hide table";
    $("table-toggle").setAttribute("aria-expanded", String(!wrap.hidden));
  });

  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => state.stats && renderCharts(state.stats), 150);
  });

  if (state.routeId) await refresh();
  // The scheduler writes at most once every few minutes; a quiet poll keeps an
  // always-open dashboard current without hammering the API.
  setInterval(() => state.routeId && refresh(), 120000);
}

// routes.js calls this after any create/edit/delete so the charts, the picker
// and the routes table can never drift apart.
window.Dashboard = { reload };

init().catch((error) => {
  $("route-line").textContent = `Could not load data: ${error.message}`;
});
