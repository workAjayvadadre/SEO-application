// Research > Trends: Google Trends interest over time, rising / top related searches, interest by region.

import { $, describeError, download, el, slug, toCsv } from "./common.js";

const SVG = "http://www.w3.org/2000/svg";
const COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)"];
const st = { data: null, busy: false };

export function initTrends({ onPlan }) {
  st.onPlan = onPlan;
  $("tr-form").addEventListener("submit", (e) => { e.preventDefault(); run(); });
  $("tr-csv").addEventListener("click", exportCsv);
  loadStates();
}

/** Open the Trends page pre-filled (used from other pages). */
export function showTrends({ keywords = [], state = "" } = {}) {
  location.hash = "#trends";
  if (keywords.length) $("tr-keywords").value = keywords.join(", ");
  if (state) $("tr-state").value = state;
}

async function loadStates() {
  try {
    const geo = await (await fetch("/static/geo.json")).json();
    $("tr-state").append(...Object.keys(geo.states).map((s) => el("option", { value: s }, s)));
  } catch { /* India-wide still works */ }
}

async function run() {
  if (st.busy) return;
  const keywords = $("tr-keywords").value.split(",").map((k) => k.trim()).filter(Boolean).slice(0, 5);
  if (!keywords.length) { note("Enter up to 5 comma-separated keywords."); return; }
  st.busy = true;
  $("tr-run").disabled = true;
  $("tr-run").textContent = "Loading…";
  note("");
  try {
    const res = await fetch("/api/trends", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keywords, state: $("tr-state").value || null, timeframe: $("tr-time").value }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(describeError(body.detail) || `Server error ${res.status}`);
    st.data = body;
    render();
  } catch (err) {
    note(err.message === "Failed to fetch" ? "Can't reach the server. Is python server.py running?" : err.message);
  } finally {
    st.busy = false;
    $("tr-run").disabled = false;
    $("tr-run").textContent = "Show trends";
  }
}

function note(text) {
  $("tr-notice").textContent = text;
  $("tr-notice").hidden = !text;
}

function render() {
  const d = st.data;
  $("tr-report").hidden = false;
  const where = d.state || "India";
  $("tr-title").textContent = `${d.keywords.join(" vs ")} · ${where}`;
  const fetched = d.fetched_at ? new Date(d.fetched_at * 1000).toLocaleString() : "";
  $("tr-sub").textContent = `${d.note}${d.cached ? ` Cached result from ${fetched} (refreshes after 12 hours).` : ""}`;

  $("tr-summary").replaceChildren(...d.summary.map((s, i) => el("div", { class: "stat" },
    el("span", { class: "stat-n" }, el("i", { class: "sw-line", style: `background:${COLORS[i]}` }), s.keyword),
    el("span", { class: `stat-l dir-${s.direction.replace(" ", "-")}` },
      s.direction === "no data" ? "no data" : `${s.direction} (${s.change_pct > 0 ? "+" : ""}${s.change_pct}%) · avg ${s.average} · peak ${s.peak}`))));
  $("tr-chart").replaceChildren(chart(d));

  $("tr-related").replaceChildren(...Object.entries(d.related).map(([kw, r]) => el("div", { class: "card" },
    el("h3", {}, `Searches related to “${kw}”`),
    el("div", { class: "two-col" },
      el("div", {}, el("h4", {}, "Rising"), list(r.rising, "Nothing rising sharply.")),
      el("div", {}, el("h4", {}, "Top"), list(r.top, "No data."))))));

  $("tr-regions").replaceChildren(d.by_region.length
    ? el("ol", { class: "region-list" }, ...d.by_region.map((r) => el("li", {}, el("span", {}, r.region),
        el("span", { class: "muted" }, r.values.join(" / ")))))
    : el("p", { class: "none" }, "Not enough data by region."));
}

function list(items, empty) {
  if (!items?.length) return el("p", { class: "none" }, empty);
  return el("ol", { class: "related" }, ...items.map((q) => el("li", {},
    el("button", { type: "button", class: "linkish", title: "Plan content for this search",
                   onclick: () => st.onPlan?.(q.query) }, q.query),
    el("span", { class: q.value === "Breakout" ? "badge b-local" : "muted" }, q.value))));
}

function chart(d) {
  const W = 760, H = 220, P = 28;
  const n = d.points.length;
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", "trend-svg");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Relative search interest over time for ${d.keywords.join(", ")}`);
  if (n < 2) return el("p", { class: "none" }, "Not enough data points.");
  const x = (i) => P + (i * (W - 2 * P)) / (n - 1);
  const y = (v) => H - P - (v / 100) * (H - 2 * P);
  for (const v of [0, 50, 100]) {
    const line = document.createElementNS(SVG, "line");
    Object.entries({ x1: P, x2: W - P, y1: y(v), y2: y(v), class: "grid" }).forEach(([k, val]) => line.setAttribute(k, val));
    const t = document.createElementNS(SVG, "text");
    Object.entries({ x: 4, y: y(v) + 4, class: "axis" }).forEach(([k, val]) => t.setAttribute(k, val));
    t.textContent = v;
    svg.append(line, t);
  }
  d.keywords.forEach((_, k) => {
    const path = document.createElementNS(SVG, "polyline");
    path.setAttribute("points", d.points.map((p, i) => `${x(i)},${y(p.values[k] ?? 0)}`).join(" "));
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", COLORS[k]);
    path.setAttribute("stroke-width", "2");
    svg.append(path);
  });
  for (const i of [0, Math.floor((n - 1) / 2), n - 1]) {
    const t = document.createElementNS(SVG, "text");
    Object.entries({ x: x(i), y: H - 6, class: "axis", "text-anchor": i === 0 ? "start" : i === n - 1 ? "end" : "middle" })
      .forEach(([k, val]) => t.setAttribute(k, val));
    t.textContent = d.points[i].time;
    svg.append(t);
  }
  return svg;
}

function exportCsv() {
  const d = st.data;
  if (!d) return;
  const rows = d.points.map((p) => Object.fromEntries([["time", p.time], ...d.keywords.map((k, i) => [k, p.values[i]])]));
  download(`trends-${slug(d.keywords.join("-"))}-${slug(d.state || "india")}-${d.timeframe}.csv`, "text/csv;charset=utf-8",
    toCsv(rows, ["time", ...d.keywords]));
}
