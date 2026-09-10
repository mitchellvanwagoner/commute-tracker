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
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
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
  const window = route.window_start
    ? ` · ${route.window_start}–${route.window_end} every ${route.interval_minutes} min · ${route.days.join(", ")}`
    : "";
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

function renderCharts(stats) {
  Charts.bandChart(
    $("chart-daily"),
    stats.daily.map((row) => ({
      label: dayLabel(row.local_date),
      min: minutes(row.min_seconds),
      max: minutes(row.max_seconds),
      avg: minutes(row.avg_seconds),
      samples: row.samples,
      date: row.local_date,
      weekday: row.weekday,
    })),
    {
      tooltip: (row) => `${WEEKDAY_NAMES[row.weekday] ?? ""} ${row.date}`.trim(),
      bandLabel: "Fastest – slowest that day",
      lineLabel: "Daily average",
      emptyMessage: "No samples yet — the first ones land during the next tracked window.",
    }
  );

  Charts.bandChart(
    $("chart-tod"),
    stats.time_of_day.map((row) => ({
      label: row.local_time,
      min: minutes(row.min_seconds),
      max: minutes(row.max_seconds),
      avg: minutes(row.avg_seconds),
      samples: row.samples,
    })),
    {
      tooltip: (row) => `Departing ${row.label}`,
      bandLabel: "Best – worst day at that time",
      lineLabel: "Average at that time",
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
  $("failures").innerHTML = failures
    .map(
      (f) =>
        `<p class="failure-row"><span class="when">${f.local_date} ${f.local_time}</span>${f.message}</p>`
    )
    .join("");
}

async function refresh() {
  const stats = await getJSON(`/api/stats?${queryString()}`);
  state.stats = stats;
  $("csv-link").href = `/api/samples.csv?${queryString()}`;
  renderSummary(stats.summary);
  renderCharts(stats);
  renderTable(stats.daily);
  renderFailures(stats.failures);
}

async function init() {
  state.routes = await getJSON("/api/routes");
  const select = $("route-select");
  select.innerHTML = state.routes
    .map((route) => `<option value="${route.id}">${route.name}</option>`)
    .join("");
  state.routeId = state.routes[0]?.id ?? null;
  select.disabled = state.routes.length < 2;
  renderRouteLine();

  select.addEventListener("change", () => {
    state.routeId = select.value;
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

  await refresh();
  // The scheduler writes at most once every few minutes; a quiet poll keeps an
  // always-open dashboard current without hammering the API.
  setInterval(refresh, 120000);
}

init().catch((error) => {
  $("route-line").textContent = `Could not load data: ${error.message}`;
});
