/* Small hand-rolled SVG chart layer.
 *
 * No chart library and no CDN: the container may well run on a home network
 * without outbound access, and the two forms this dashboard needs (a min-max
 * band with an average line, and a column chart) are a few dozen lines each.
 *
 * Marks follow one spec throughout: 2px lines, >=8px markers with a 2px surface
 * ring, <=24px columns with a 4px rounded cap, hairline solid gridlines, and a
 * hover crosshair + tooltip on every plot.
 */
const SVG_NS = "http://www.w3.org/2000/svg";

const Charts = (() => {
  const tooltipEl = () => document.getElementById("tooltip");

  function el(name, attrs = {}) {
    const node = document.createElementNS(SVG_NS, name);
    for (const [key, value] of Object.entries(attrs)) {
      if (value !== null && value !== undefined) node.setAttribute(key, value);
    }
    return node;
  }

  function cssVar(root, name) {
    return getComputedStyle(root).getPropertyValue(name).trim();
  }

  /** Round a raw axis step up to 1/2/5 x 10^n so ticks land on clean numbers. */
  function niceStep(raw) {
    const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
    const scaled = raw / magnitude;
    const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
    return step * magnitude;
  }

  function niceScale(lo, hi, targetTicks = 5) {
    if (!isFinite(lo) || !isFinite(hi)) return { min: 0, max: 1, ticks: [0, 1] };
    if (hi === lo) {
      hi = lo + Math.max(1, Math.abs(lo) * 0.1);
    }
    const step = niceStep((hi - lo) / targetTicks);
    const min = Math.max(0, Math.floor(lo / step) * step);
    const max = Math.ceil(hi / step) * step;
    const ticks = [];
    for (let value = min; value <= max + step / 2; value += step) ticks.push(Number(value.toFixed(6)));
    return { min, max, ticks };
  }

  /** Keep at most `max` x labels so they never collide. */
  function labelStride(count, max) {
    return Math.max(1, Math.ceil(count / max));
  }

  function showTooltip(event, html) {
    const tip = tooltipEl();
    tip.innerHTML = html;
    tip.hidden = false;
    const pad = 14;
    const rect = tip.getBoundingClientRect();
    let left = event.clientX + pad;
    let top = event.clientY + pad;
    if (left + rect.width > window.innerWidth - 8) left = event.clientX - rect.width - pad;
    if (top + rect.height > window.innerHeight - 8) top = event.clientY - rect.height - pad;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, top)}px`;
  }

  function hideTooltip() {
    tooltipEl().hidden = true;
  }

  function emptyState(container, message) {
    container.innerHTML = `<p class="empty">${message}</p>`;
  }

  function legend(items) {
    const wrap = document.createElement("div");
    wrap.className = "legend";
    for (const item of items) {
      const node = document.createElement("span");
      node.className = "legend-item";
      const key = document.createElement("span");
      key.className = `legend-key${item.type === "line" ? " line" : ""}`;
      key.style.background = item.color;
      node.append(key, document.createTextNode(item.label));
      wrap.append(node);
    }
    return wrap;
  }

  function frame(container, { height = 260, margin }) {
    const width = Math.max(320, container.clientWidth || 640);
    const svg = el("svg", {
      viewBox: `0 0 ${width} ${height}`,
      width,
      height,
      role: "img",
    });
    svg.style.width = "100%";
    svg.style.height = "auto";
    return {
      svg,
      width,
      height,
      innerW: width - margin.left - margin.right,
      innerH: height - margin.top - margin.bottom,
    };
  }

  function axes(svg, geo, margin, scale, colors, formatY) {
    for (const tick of scale.ticks) {
      const y = margin.top + geo.innerH * (1 - (tick - scale.min) / (scale.max - scale.min));
      svg.append(
        el("line", {
          x1: margin.left,
          x2: margin.left + geo.innerW,
          y1: y,
          y2: y,
          stroke: tick === scale.min ? colors.axis : colors.grid,
          "stroke-width": 1,
        })
      );
      const label = el("text", {
        x: margin.left - 8,
        y: y + 4,
        "text-anchor": "end",
        fill: colors.muted,
        "font-size": 11,
      });
      label.textContent = formatY(tick);
      svg.append(label);
    }
  }

  function xLabels(svg, geo, margin, labels, xAt, colors) {
    const stride = labelStride(labels.length, Math.max(3, Math.floor(geo.innerW / 74)));
    labels.forEach((text, index) => {
      if (index % stride !== 0 && index !== labels.length - 1) return;
      const node = el("text", {
        x: xAt(index),
        y: margin.top + geo.innerH + 18,
        "text-anchor": "middle",
        fill: colors.muted,
        "font-size": 11,
      });
      node.textContent = text;
      svg.append(node);
    });
  }

  /**
   * Min-max band with an average line.
   * rows: [{ label, min, max, avg, ...extra }]
   */
  function bandChart(container, rows, options = {}) {
    const {
      formatValue = (v) => v.toFixed(1),
      unit = "min",
      tooltip = (row) => row.label,
      bandLabel = "Fastest - slowest",
      lineLabel = "Average",
      emptyMessage = "No samples yet.",
    } = options;

    container.innerHTML = "";
    if (!rows.length) return emptyState(container, emptyMessage);

    const colors = {
      series: cssVar(container, "--series-1"),
      wash: cssVar(container, "--series-1-wash"),
      grid: cssVar(container, "--grid"),
      axis: cssVar(container, "--axis"),
      muted: cssVar(container, "--text-muted"),
      surface: cssVar(container, "--surface-1"),
    };

    container.append(
      legend([
        { label: bandLabel, color: colors.wash },
        { label: lineLabel, color: colors.series, type: "line" },
      ])
    );

    const margin = { top: 14, right: 16, bottom: 34, left: 52 };
    const geo = frame(container, { margin });
    const { svg, innerW, innerH } = geo;

    const scale = niceScale(
      Math.min(...rows.map((r) => r.min)),
      Math.max(...rows.map((r) => r.max))
    );
    const xAt = (index) =>
      rows.length === 1
        ? margin.left + innerW / 2
        : margin.left + (innerW * index) / (rows.length - 1);
    const yAt = (value) =>
      margin.top + innerH * (1 - (value - scale.min) / (scale.max - scale.min));

    axes(svg, geo, margin, scale, colors, (v) => formatValue(v));

    // Band: forward along the maxima, back along the minima.
    const forward = rows.map((row, i) => `${i === 0 ? "M" : "L"}${xAt(i)},${yAt(row.max)}`);
    const backward = rows
      .map((row, i) => ({ row, i }))
      .reverse()
      .map(({ row, i }) => `L${xAt(i)},${yAt(row.min)}`);
    svg.append(
      el("path", {
        d: `${forward.join("")}${backward.join("")}Z`,
        fill: colors.wash,
        stroke: "none",
      })
    );

    const linePath = rows.map((row, i) => `${i === 0 ? "M" : "L"}${xAt(i)},${yAt(row.avg)}`).join("");
    svg.append(
      el("path", {
        d: linePath,
        fill: "none",
        stroke: colors.series,
        "stroke-width": 2,
        "stroke-linejoin": "round",
        "stroke-linecap": "round",
      })
    );

    // Markers only while they can breathe; past that the line carries the shape.
    if (rows.length <= 40) {
      rows.forEach((row, i) => {
        svg.append(
          el("circle", {
            cx: xAt(i),
            cy: yAt(row.avg),
            r: 4,
            fill: colors.series,
            stroke: colors.surface,
            "stroke-width": 2,
          })
        );
      });
    }

    xLabels(svg, geo, margin, rows.map((r) => r.label), xAt, colors);

    // Hover layer: crosshair + emphasized marker on the nearest column.
    const crosshair = el("line", {
      y1: margin.top,
      y2: margin.top + innerH,
      stroke: colors.axis,
      "stroke-width": 1,
      opacity: 0,
    });
    const focus = el("circle", {
      r: 5,
      fill: colors.series,
      stroke: colors.surface,
      "stroke-width": 2,
      opacity: 0,
    });
    svg.append(crosshair, focus);

    const overlay = el("rect", {
      x: margin.left,
      y: margin.top,
      width: innerW,
      height: innerH,
      fill: "transparent",
    });
    overlay.style.cursor = "crosshair";
    const nearest = (event) => {
      const box = svg.getBoundingClientRect();
      const scaleX = geo.width / box.width;
      const x = (event.clientX - box.left) * scaleX;
      const ratio = rows.length === 1 ? 0 : (x - margin.left) / innerW;
      return Math.max(0, Math.min(rows.length - 1, Math.round(ratio * (rows.length - 1))));
    };
    overlay.addEventListener("mousemove", (event) => {
      const index = nearest(event);
      const row = rows[index];
      crosshair.setAttribute("x1", xAt(index));
      crosshair.setAttribute("x2", xAt(index));
      crosshair.setAttribute("opacity", 1);
      focus.setAttribute("cx", xAt(index));
      focus.setAttribute("cy", yAt(row.avg));
      focus.setAttribute("opacity", 1);
      showTooltip(
        event,
        `<div class="t-title">${tooltip(row)}</div>` +
          `<div class="t-row">Average <b>${formatValue(row.avg)} ${unit}</b></div>` +
          `<div class="t-row">Fastest <b>${formatValue(row.min)}</b> &middot; Slowest <b>${formatValue(row.max)}</b></div>` +
          `<div class="t-row">${row.samples ?? 0} sample${row.samples === 1 ? "" : "s"}</div>`
      );
    });
    overlay.addEventListener("mouseleave", () => {
      crosshair.setAttribute("opacity", 0);
      focus.setAttribute("opacity", 0);
      hideTooltip();
    });
    svg.append(overlay);
    container.append(svg);
  }

  /** Column chart with a rounded cap; rows: [{ label, value, ...extra }] */
  function barChart(container, rows, options = {}) {
    const {
      formatValue = (v) => v.toFixed(1),
      unit = "min",
      tooltip = (row) => row.label,
      emptyMessage = "No samples yet.",
    } = options;

    container.innerHTML = "";
    if (!rows.length) return emptyState(container, emptyMessage);

    const colors = {
      series: cssVar(container, "--series-1"),
      grid: cssVar(container, "--grid"),
      axis: cssVar(container, "--axis"),
      muted: cssVar(container, "--text-muted"),
      primary: cssVar(container, "--text-primary"),
      surface: cssVar(container, "--surface-1"),
    };

    const margin = { top: 22, right: 16, bottom: 34, left: 52 };
    const geo = frame(container, { margin, height: 240 });
    const { svg, innerW, innerH } = geo;

    const scale = niceScale(0, Math.max(...rows.map((r) => r.value)));
    const band = innerW / rows.length;
    const thickness = Math.min(24, band * 0.55);
    const yAt = (value) =>
      margin.top + innerH * (1 - (value - scale.min) / (scale.max - scale.min));

    axes(svg, geo, margin, scale, colors, (v) => formatValue(v));

    rows.forEach((row, index) => {
      const cx = margin.left + band * (index + 0.5);
      const top = yAt(row.value);
      const baseline = margin.top + innerH;
      const radius = Math.min(4, thickness / 2, Math.max(0, baseline - top));
      const left = cx - thickness / 2;
      const right = cx + thickness / 2;
      // Rounded at the data end, square at the baseline.
      const path = el("path", {
        d:
          `M${left},${baseline}L${left},${top + radius}` +
          `Q${left},${top} ${left + radius},${top}` +
          `L${right - radius},${top}Q${right},${top} ${right},${top + radius}` +
          `L${right},${baseline}Z`,
        fill: colors.series,
      });
      svg.append(path);

      const value = el("text", {
        x: cx,
        y: top - 8,
        "text-anchor": "middle",
        fill: colors.primary,
        "font-size": 11,
        "font-weight": 600,
      });
      value.textContent = formatValue(row.value);
      svg.append(value);

      const hit = el("rect", {
        x: cx - band / 2,
        y: margin.top,
        width: band,
        height: innerH,
        fill: "transparent",
      });
      hit.addEventListener("mousemove", (event) =>
        showTooltip(
          event,
          `<div class="t-title">${tooltip(row)}</div>` +
            `<div class="t-row">Average <b>${formatValue(row.value)} ${unit}</b></div>` +
            (row.min !== undefined
              ? `<div class="t-row">Fastest <b>${formatValue(row.min)}</b> &middot; Slowest <b>${formatValue(row.max)}</b></div>`
              : "") +
            `<div class="t-row">${row.samples ?? 0} sample${row.samples === 1 ? "" : "s"}</div>`
        )
      );
      hit.addEventListener("mouseleave", hideTooltip);
      svg.append(hit);
    });

    xLabels(svg, geo, margin, rows.map((r) => r.label), (i) => margin.left + band * (i + 0.5), colors);
    container.append(svg);
  }

  return { bandChart, barChart, hideTooltip };
})();
