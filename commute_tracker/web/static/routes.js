/* Route management: the table of routes, the editor dialog, and delete.
 *
 * Every mutation goes through the API and then calls Dashboard.reload(), so the
 * charts and the route picker always reflect what the scheduler is actually
 * tracking -- the server reschedules the route as part of the same request.
 */

const DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_LABEL = {
  mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun",
};

const Routes = (() => {
  const el = (id) => document.getElementById(id);
  let editing = null; // the route being edited, or null when adding
  let pendingDelete = null;

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const text = await response.text();
    const payload = text ? JSON.parse(text) : null;
    if (!response.ok) {
      throw new Error(payload?.detail || `${response.status} ${response.statusText}`);
    }
    return payload;
  }

  function summarizeDays(days) {
    if (!days?.length) return "—";
    const set = new Set(days);
    const weekdays = ["mon", "tue", "wed", "thu", "fri"];
    if (days.length === 7) return "Every day";
    if (weekdays.every((d) => set.has(d)) && days.length === 5) return "Weekdays";
    if (days.length === 2 && set.has("sat") && set.has("sun")) return "Weekends";
    return days.map((d) => DAY_LABEL[d]).join(", ");
  }

  function renderTable(routes) {
    if (!routes.length) {
      el("routes-table").innerHTML =
        '<p class="empty">No routes yet. Add one to start tracking a commute.</p>';
      return;
    }
    const rows = routes
      .map((route) => {
        const editable = route.configured;
        const window = editable
          ? `${route.window_start}–${route.window_end} / ${route.interval_minutes}m`
          : "—";
        const state = !editable
          ? '<span class="tag">history only</span>'
          : route.enabled
            ? '<span class="tag on">tracking</span>'
            : '<span class="tag off">paused</span>';
        const actions = editable
          ? `<button class="link" data-edit="${route.id}">Edit</button>
             <button class="link" data-copy="${route.id}" title="Add the return leg of this commute">Copy reversed</button>
             <button class="link" data-toggle="${route.id}">${route.enabled ? "Pause" : "Resume"}</button>
             <button class="link danger" data-delete="${route.id}">Delete</button>`
          : `<button class="link danger" data-delete="${route.id}">Delete data</button>`;
        return `<tr>
          <td>
            <div class="route-name">${escapeHtml(route.name)}</div>
            <div class="route-path">${escapeHtml(route.origin)} → ${escapeHtml(route.destination)}</div>
          </td>
          <td>${window}</td>
          <td>${editable ? summarizeDays(route.days) : "—"}</td>
          <td>${route.notify_at || "—"}</td>
          <td>${(route.samples ?? 0).toLocaleString()}</td>
          <td>${state}</td>
          <td class="actions">${actions}</td>
        </tr>`;
      })
      .join("");

    el("routes-table").innerHTML = `<div class="table-scroll"><table>
      <thead><tr>
        <th>Route</th><th>Window</th><th>Days</th><th>Report</th>
        <th>Samples</th><th>Status</th><th></th>
      </tr></thead>
      <tbody>${rows}</tbody></table></div>`;

    el("routes-table").querySelectorAll("[data-edit]").forEach((button) => {
      button.addEventListener("click", () =>
        openEditor(routes.find((r) => r.id === button.dataset.edit))
      );
    });
    el("routes-table").querySelectorAll("[data-copy]").forEach((button) => {
      button.addEventListener("click", () =>
        openEditor(routes.find((r) => r.id === button.dataset.copy), { duplicate: true })
      );
    });
    el("routes-table").querySelectorAll("[data-toggle]").forEach((button) => {
      button.addEventListener("click", async () => {
        const route = routes.find((r) => r.id === button.dataset.toggle);
        await api(`/api/routes/${route.id}`, {
          method: "PATCH",
          body: JSON.stringify({ enabled: !route.enabled }),
        });
        Dashboard.reload();
      });
    });
    el("routes-table").querySelectorAll("[data-delete]").forEach((button) => {
      button.addEventListener("click", () =>
        confirmDelete(routes.find((r) => r.id === button.dataset.delete))
      );
    });
  }

  function escapeHtml(value) {
    const node = document.createElement("span");
    node.textContent = value ?? "";
    return node.innerHTML;
  }

  // ------------------------------------------------------------------ editor

  /** Open the editor.
   *
   * ``duplicate`` opens a *new* route pre-filled from an existing one with the
   * addresses reversed, which is almost always what copying a commute is for:
   * the same trip, the other way, later in the day. It deliberately does not
   * carry the id -- the copy has to collect its own history.
   */
  function openEditor(route, { duplicate = false } = {}) {
    editing = duplicate ? null : (route ?? null);
    const form = el("route-form");
    form.reset();
    el("editor-title").textContent = duplicate
      ? "Copy route (reversed)"
      : route
        ? "Edit route"
        : "Add a route";
    el("route-delete").hidden = duplicate || !route;
    setTestResult("");

    const defaults = {
      name: "",
      origin: "",
      destination: "",
      window_start: "07:00",
      window_end: "09:00",
      interval_minutes: 15,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      notify_at: "",
      days: ["mon", "tue", "wed", "thu", "fri"],
      enabled: true,
    };
    const source = route || {};
    const values = {
      ...defaults,
      ...source,
      ...(duplicate
        ? {
            name: `${source.name ?? "Commute"} (return)`,
            origin: source.destination ?? "",
            destination: source.origin ?? "",
          }
        : {}),
    };
    for (const field of ["name", "origin", "destination", "window_start", "window_end",
                         "interval_minutes", "timezone", "notify_at"]) {
      form.elements[field].value = values[field] ?? "";
    }
    form.elements.enabled.checked = values.enabled !== false;
    const selected = new Set(values.days || []);
    form.querySelectorAll("[name=days]").forEach((box) => {
      box.checked = selected.has(box.value);
    });

    el("route-editor").showModal();
    form.elements.name.focus();
    refreshCost();
  }

  function readForm() {
    const form = el("route-form");
    const days = [...form.querySelectorAll("[name=days]:checked")].map((box) => box.value);
    return {
      name: form.elements.name.value.trim(),
      origin: form.elements.origin.value.trim(),
      destination: form.elements.destination.value.trim(),
      window_start: form.elements.window_start.value,
      window_end: form.elements.window_end.value,
      interval_minutes: Number(form.elements.interval_minutes.value),
      timezone: form.elements.timezone.value.trim(),
      notify_at: form.elements.notify_at.value || null,
      days: days.sort((a, b) => DAY_ORDER.indexOf(a) - DAY_ORDER.indexOf(b)),
      enabled: form.elements.enabled.checked,
    };
  }

  // ---------------------------------------------------------- address lookup

  /** Fill a field's datalist with suggestions for what has been typed so far.
   *
   * Debounced, because the provider is a shared free service (and, if someone
   * has switched it to Google, a metered one): a request per keystroke would be
   * rude in the first case and expensive in the second. Failures are swallowed
   * -- suggestions are a convenience, and the field still takes free text.
   */
  const suggestTimers = {};
  const suggestCache = new Map();

  function attachAddressLookup(fieldName, datalistId) {
    const input = el("route-form").elements[fieldName];
    const list = el(datalistId);

    input.addEventListener("input", () => {
      const query = input.value.trim();
      clearTimeout(suggestTimers[fieldName]);
      if (query.length < 3) {
        list.innerHTML = "";
        return;
      }
      suggestTimers[fieldName] = setTimeout(async () => {
        try {
          let options = suggestCache.get(query);
          if (!options) {
            const result = await api(`/api/addresses?q=${encodeURIComponent(query)}`);
            options = result.suggestions || [];
            // Bounded so a long editing session cannot grow without limit.
            if (suggestCache.size > 50) suggestCache.clear();
            suggestCache.set(query, options);
          }
          list.innerHTML = options
            .map((address) => `<option value="${escapeHtml(address)}"></option>`)
            .join("");
        } catch {
          list.innerHTML = "";
        }
      }, 350);
    });
  }

  /** Swap From and To -- the fast path to the return leg of the same commute. */
  function swapAddresses() {
    const form = el("route-form");
    const { origin, destination } = form.elements;
    [origin.value, destination.value] = [destination.value, origin.value];
    setTestResult("");
  }

  /** Cost the route being edited, before it is committed. Spends no API calls. */
  async function refreshCost() {
    const node = el("route-cost");
    const payload = readForm();
    if (!payload.origin || !payload.destination) payload.origin = payload.destination = "preview";
    try {
      const preview = await api("/api/routes/preview", {
        method: "POST",
        body: JSON.stringify({ ...payload, id: editing?.id ?? "" }),
      });
      const perMonth = preview.calls_per_month.toLocaleString();
      node.textContent =
        `${preview.samples_per_day} samples/day · about ${perMonth} Routes API calls a month.` +
        (preview.warning ? ` ${preview.warning}` : "");
      node.className = preview.warning ? "route-cost warn" : "route-cost";
    } catch {
      // An incomplete or invalid form has nothing meaningful to cost yet; the
      // save path reports the validation error properly.
      node.textContent = "";
      node.className = "route-cost";
    }
  }

  function setTestResult(message, tone = "") {
    const node = el("route-test-result");
    node.textContent = message;
    node.className = `test-result ${tone}`;
  }

  async function save(event) {
    event.preventDefault();
    const payload = readForm();
    if (!payload.days.length) return setTestResult("Pick at least one day.", "bad");
    const button = el("route-save");
    button.disabled = true;
    try {
      const saved = editing
        ? await api(`/api/routes/${editing.id}`, {
            method: "PATCH",
            body: JSON.stringify(payload),
          })
        : await api("/api/routes", { method: "POST", body: JSON.stringify(payload) });
      el("route-editor").close();
      Dashboard.reload();
      // The route is saved either way -- but an overage must not slip past
      // unseen just because the dialog closed.
      if (saved?.warning) window.alert(`Heads up

${saved.warning}`);
    } catch (error) {
      setTestResult(error.message, "bad");
    } finally {
      button.disabled = false;
    }
  }

  /** Prove both addresses resolve, and that the API key works, before saving. */
  async function test() {
    const { origin, destination } = readForm();
    if (!origin || !destination) return setTestResult("Enter both addresses first.", "bad");
    const button = el("route-test");
    button.disabled = true;
    setTestResult("Checking…");
    try {
      const result = await api("/api/routes/validate", {
        method: "POST",
        body: JSON.stringify({ origin, destination }),
      });
      const minutes = (result.duration_seconds / 60).toFixed(1);
      const km = result.distance_meters ? ` over ${(result.distance_meters / 1000).toFixed(1)} km` : "";
      setTestResult(`Right now: ${minutes} min${km}.`, "good");
    } catch (error) {
      setTestResult(error.message, "bad");
    } finally {
      button.disabled = false;
    }
  }

  // ------------------------------------------------------------------ delete

  function confirmDelete(route) {
    pendingDelete = route;
    const samples = (route.samples ?? 0).toLocaleString();
    el("delete-text").textContent = route.configured
      ? `Stop tracking "${route.name}"? It has ${samples} recorded samples.`
      : `"${route.name}" is no longer tracked. Delete its ${samples} recorded samples?`;
    el("delete-keep").hidden = !route.configured;
    el("route-delete-dialog").showModal();
  }

  async function doDelete(dropHistory) {
    const route = pendingDelete;
    if (!route) return;
    // A route with history that is only being retired keeps its samples, so the
    // charts do not lose the past; dropping data is always the explicit choice.
    await api(`/api/routes/${route.id}?drop_history=${dropHistory}`, { method: "DELETE" });
    el("route-delete-dialog").close();
    pendingDelete = null;
    Dashboard.reload();
  }

  function init() {
    el("route-add").addEventListener("click", () => openEditor(null));
    el("route-swap").addEventListener("click", swapAddresses);
    attachAddressLookup("origin", "origin-options");
    attachAddressLookup("destination", "destination-options");
    for (const field of ["window_start", "window_end", "interval_minutes", "enabled"]) {
      el("route-form").elements[field].addEventListener("change", refreshCost);
    }
    el("route-form")
      .querySelectorAll("[name=days]")
      .forEach((box) => box.addEventListener("change", refreshCost));
    el("route-form").addEventListener("submit", save);
    el("route-test").addEventListener("click", test);
    el("route-cancel").addEventListener("click", () => el("route-editor").close());
    el("route-delete").addEventListener("click", () => {
      el("route-editor").close();
      confirmDelete(editing);
    });
    el("delete-keep").addEventListener("click", () => doDelete(false));
    el("delete-drop").addEventListener("click", () => doDelete(true));
    el("delete-cancel").addEventListener("click", () => el("route-delete-dialog").close());

    // Offer the browser's known timezones where the browser can list them.
    if (typeof Intl.supportedValuesOf === "function") {
      const list = el("timezone-options");
      for (const zone of Intl.supportedValuesOf("timeZone")) {
        const option = document.createElement("option");
        option.value = zone;
        list.append(option);
      }
    }
  }

  return { init, renderTable, openEditor };
})();
