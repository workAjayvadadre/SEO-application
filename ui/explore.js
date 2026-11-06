// Keyword explorer, laid out like an AnswerThePublic dashboard:
//   #searches        → search bar + table of saved searches (stored by the server)
//   #search/<id>     → report: Questions, Prepositions, Comparisons, Alphabeticals, Local,
//                      each as a radial visualization or as data columns.
// Keyword text is external (Google autocomplete): it is only ever inserted as text.

import { $, describeError, download, el, safeUrl, slug, toCsv } from "./common.js";
import { intentBar } from "./keywords.js";

const SVG = "http://www.w3.org/2000/svg";
const GROUPS = [
  { id: "questions", label: "Questions", blurb: "are, can I, how do, should I, what, when, where, which, who, why, will…" },
  { id: "past", label: "In the past", blurb: "did, was: what people ask about earlier experiences" },
  { id: "prepositions", label: "Prepositions", blurb: "can, for, is, near, to, with, without" },
  { id: "comparisons", label: "Comparisons", blurb: "vs, or, and, like, best, better, top, review, rating, price, cost, cheap, recommended…" },
  { id: "alphabetical", label: "Letters", blurb: "every letter a to z, before and after your keyword" },
  { id: "query", label: "Query", blurb: "Google's own completions for your keyword" },
  { id: "local", label: "Local", blurb: "searches tied to the city or region you picked" },
];
const MAX_LEAVES = 100; // more than this and wheel labels collide; Data view shows everything
const REGION_OK = /^[A-Za-z][A-Za-z0-9 .&'-]*$/;
const COUNTRY = { in: "India", us: "United States", gb: "United Kingdom", ae: "UAE", ca: "Canada", au: "Australia", sg: "Singapore" };

const st = {
  data: null, source: "google", views: {}, menuItem: null, timer: null, history: [], historyFilter: "",
  onScan: () => {}, onPlan: () => {},
};

// ---------------------------------------------------------------- setup & routing

export function initExplore({ onScan, onPlan }) {
  st.onScan = onScan;
  st.onPlan = onPlan;
  $("ex-form").addEventListener("submit", (e) => { e.preventDefault(); run(); });
  for (const btn of $("ex-source").querySelectorAll("[data-source]")) {
    btn.addEventListener("click", () => setSource(btn.dataset.source));
  }
  $("ex-history-filter").addEventListener("input", (e) => { st.historyFilter = e.target.value; renderHistory(); });
  $("ex-csv").addEventListener("click", () => exportCsv());
  $("ex-ai").addEventListener("click", () => void loadAiQuestions());
  $("ex-xlsx").addEventListener("click", () => exportAnswerSocrates());
  $("ex-copy").addEventListener("click", (e) => copyText(st.data.keywords.map((k) => k.keyword).join("\n"), e.currentTarget));
  $("ex-plan").addEventListener("click", () => st.onPlan({ keyword: st.data.keyword, country: st.data.country, region: st.data.region }));
  $("ex-menu-copy").addEventListener("click", (e) => copyText(st.menuItem?.keyword || "", e.currentTarget));
  $("ex-menu-explore").addEventListener("click", () => { $("ex-menu").close(); exploreKeyword(st.menuItem.keyword); });
  $("ex-menu-scan").addEventListener("click", () => { $("ex-menu").close(); st.onScan(st.menuItem.keyword); });
  $("ex-menu-plan").addEventListener("click", () => {
    $("ex-menu").close();
    st.onPlan({ keyword: st.menuItem.keyword, country: st.data.country, region: st.data.region });
  });
}

/** True when the hash belongs to the explorer; shows the right page. Called by app.js on every hash change. */
export function routeExplore(hash) {
  const report = /^#search\/([0-9a-f]{32})$/.exec(hash);
  if (!report && !["", "#", "#searches", "#explore"].includes(hash)) return false;
  $("ex-home").hidden = Boolean(report);
  $("ex-report-page").hidden = !report;
  if (report) openSearch(report[1]);
  else loadHistory();
  return true;
}

function setSource(source) {
  st.source = source;
  for (const btn of $("ex-source").querySelectorAll("[data-source]")) {
    btn.setAttribute("aria-checked", String(btn.dataset.source === source));
  }
}

function notice(message) {
  $("ex-notice").hidden = !message;
  $("ex-notice").textContent = message || "";
  $("ex-notice").classList.toggle("error", Boolean(message));
}

export function exploreKeyword(keyword) {
  location.hash = "#searches";
  $("ex-input").value = keyword;
  run();
}

async function api(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(describeError(err.detail) || `Server error ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

// ---------------------------------------------------------------- searches (history)

async function loadHistory() {
  try {
    st.history = (await api("/api/searches")).searches;
  } catch {
    st.history = [];
  }
  renderHistory();
}

function ago(isoDate) {
  const s = (Date.now() - new Date(isoDate).getTime()) / 1000;
  if (!Number.isFinite(s)) return "";
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.round(s / 86400)} d ago`;
  return new Date(isoDate).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

const place = (h) => [h.region, COUNTRY[h.country] || (h.country || "").toUpperCase()].filter(Boolean).join(", ");

function renderHistory() {
  const needle = st.historyFilter.trim().toLowerCase();
  const rows = st.history.filter((h) => !needle || `${h.keyword} ${h.region || ""}`.toLowerCase().includes(needle));
  $("ex-history-count").textContent = st.history.length ? String(st.history.length) : "";
  $("ex-history-empty").hidden = rows.length > 0;
  $("ex-history-empty").textContent = st.history.length
    ? "No searches match the filter." : "No searches yet. Enter a keyword above to run your first search.";
  $("ex-history").replaceChildren(...rows.map((h) => el("tr", {},
    el("td", { class: "kw-cell" }, el("a", { href: `#search/${h.id}` }, h.keyword)),
    el("td", {}, el("span", { class: "engine" }, h.source === "youtube" ? "YouTube" : "Google")),
    el("td", {}, place(h)),
    el("td", {}, h.language === "hi" ? "Hindi" : "English"),
    el("td", { class: "num" }, h.total_keywords ?? ""),
    el("td", { class: "muted", title: new Date(h.created_at).toLocaleString() }, ago(h.created_at)),
    el("td", {}, el("div", { class: "row-actions" },
      el("a", { class: "btn btn-ghost btn-sm", href: `#search/${h.id}` }, "Open"),
      el("button", { type: "button", class: "btn btn-ghost btn-sm", title: "Run this search again for fresh results",
        onclick: () => rerun(h) }, "Re-run"),
      el("button", { type: "button", class: "btn btn-ghost btn-sm danger", "aria-label": `Delete search ${h.keyword}`,
        onclick: () => removeSearch(h) }, "Delete"))))));
}

function rerun(h) {
  $("ex-input").value = h.keyword;
  $("ex-country").value = COUNTRY[h.country] ? h.country : "in";
  $("ex-language").value = h.language === "hi" ? "hi" : "en";
  $("ex-region").value = h.region || "";
  setSource(h.source === "youtube" ? "youtube" : "google");
  window.scrollTo({ top: 0, behavior: "smooth" });
  run();
}

async function removeSearch(h) {
  if (!window.confirm(`Delete the search “${h.keyword}”?`)) return;
  try {
    await api(`/api/searches/${h.id}`, { method: "DELETE" });
  } catch (err) {
    notice(err.message);
  }
  loadHistory();
}

// ---------------------------------------------------------------- run a new search

async function run() {
  notice("");
  const keyword = $("ex-input").value.trim();
  if (!keyword) {
    notice("Enter a keyword to search.");
    $("ex-input").focus();
    return;
  }
  const region = $("ex-region").value.trim() || null;
  if (region && !REGION_OK.test(region)) {
    notice("City or region can only contain letters, numbers, spaces, '.', '&', ''' and '-'.");
    return;
  }
  const body = {
    keyword, region, source: st.source, country: $("ex-country").value, language: $("ex-language").value,
    include_serp: $("ex-serp").checked && st.source === "google",
  };
  setBusy(true, keyword);
  try {
    const data = await api("/api/keywords", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    st.data = data;
    st.views = {};
    location.hash = `#search/${data.id}`;
  } catch (err) {
    notice(err.message === "Failed to fetch" ? "Can't reach the server. Is python server.py running?" : err.message);
  } finally {
    setBusy(false);
  }
}

function setBusy(busy, keyword = "") {
  $("ex-run").disabled = busy;
  $("ex-progress").hidden = !busy;
  clearInterval(st.timer);
  if (!busy) return;
  const started = Date.now();
  const where = st.source === "youtube" ? "YouTube" : "Google";
  const tick = () => {
    $("ex-progress-text").textContent =
      ` “${keyword}” · ${where} autocomplete · ${Math.round((Date.now() - started) / 1000)}s (usually 5–15s)`;
  };
  tick();
  st.timer = setInterval(tick, 1000);
}

// ---------------------------------------------------------------- report

async function openSearch(id) {
  window.scrollTo({ top: 0 });
  if (st.data?.id === id) {
    render();
    return;
  }
  $("ex-report").hidden = true;
  $("ex-loading").hidden = false;
  $("ex-crumb").textContent = "";
  try {
    st.data = await api(`/api/searches/${id}`);
    st.views = {};
    render();
  } catch {
    $("ex-crumb").textContent = "Search not found. It may have been deleted.";
  } finally {
    $("ex-loading").hidden = true;
  }
}

function render() {
  const d = st.data;
  $("ex-report").hidden = false;
  const where = d.source === "youtube" ? "YouTube" : "Google";
  $("ex-crumb").textContent = d.keyword;
  $("ex-title").textContent = d.keyword;
  $("ex-sub").textContent = [
    `${where} · ${place(d)}`,
    d.language === "hi" ? "Hindi" : "English",
    d.created_at ? `searched ${ago(d.created_at)}` : null,
    d.requests?.failed ? `${d.requests.failed} of ${d.requests.sent} requests failed` : null,
  ].filter(Boolean).join(" · ");

  const present = GROUPS.filter((g) => d.counts[g.id]);
  const jump = (id) => (e) => { e.preventDefault(); $(id).scrollIntoView({ behavior: "smooth", block: "start" }); };
  $("ex-stats").replaceChildren(
    el("div", { class: "stat stat-total" }, el("span", { class: "stat-n" }, d.total_keywords), el("span", { class: "stat-l" }, "Search ideas")),
    ...present.map((g) => el("a", { class: "stat", href: `#ex-${g.id}`, onclick: jump(`ex-${g.id}`) },
      el("span", { class: "stat-n" }, d.counts[g.id]), el("span", { class: "stat-l" }, g.label))),
    intentBar(d.intents));

  const sections = present.map(groupSection);
  const links = present.map((g) => [g.label, `ex-${g.id}`, d.counts[g.id]]);
  if (d.clusters?.length) { sections.push(topicsSection()); links.push(["Topics", "ex-topics", null]); }
  if (d.people_also_ask?.length || d.related_searches?.length) {
    sections.push(paaSection());
    links.push([d.people_also_ask?.length ? "People also ask" : "Related", "ex-paa", null]);
  }
  if (d.ai_questions?.questions?.length) {
    sections.push(aiSection());
    links.push(["AI assistants (est.)", "ex-ai-questions", d.ai_questions.questions.length]);
  }
  if (!present.length) sections.push(el("section", { class: "card" }, el("p", { class: "none" },
    "Autocomplete returned nothing containing this keyword. Try a shorter or more common phrase.")));
  $("ex-jump").replaceChildren(...links.map(([label, id, n]) =>
    el("a", { href: `#${id}`, onclick: jump(id) }, label, n != null ? el("span", { class: "n" }, n) : null)));
  $("ex-sections").replaceChildren(...sections);
}

const narrow = () => window.matchMedia("(max-width: 700px)").matches;

// ---------------------------------------------------------------- AI-assistant questions (estimated)

async function loadAiQuestions() {
  const d = st.data;
  if (!d) return;
  const button = $("ex-ai");
  button.disabled = true;
  button.textContent = "Asking…";
  try {
    d.ai_questions = await api("/api/ai-questions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keyword: d.keyword, country: d.country || "in", region: d.region || null }),
    });
    render();
    $("ex-ai-questions")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    notice(err.message);
  } finally {
    button.disabled = false;
    button.textContent = "AI-assistant questions";
  }
}

function aiSection() {
  const a = st.data.ai_questions;
  return el("section", { class: "card", id: "ex-ai-questions" },
    el("h2", {}, "AI-assistant questions ", el("span", { class: "badge b-nmc" }, "estimated")),
    el("p", { class: "card-sub muted" }, `${a.note} Included in the AnswerSocrates-format Excel as “ai assistants (estimated)”.`),
    el("div", { class: "table-wrap" }, el("table", { class: "data-table" },
      el("thead", {}, el("tr", {}, ...["Question", "Stage", "Label", "Why people ask", "NMC"].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...a.questions.map((q) => el("tr", {},
        el("td", {}, q.question), el("td", { class: "nowrap" }, q.stage), el("td", {}, q.label),
        el("td", {}, q.why_people_ask), el("td", {}, q.nmc_note ? el("span", { class: "badge b-nmc" }, q.nmc_note) : "")))))));
}

function groupSection(group) {
  const modifiers = st.data.groups[group.id] || [];
  const total = st.data.counts[group.id];
  const view = st.views[group.id] || (narrow() || group.id === "alphabetical" ? "data" : "visual");
  const body = el("div", { class: "ex-body" });
  const toggle = el("div", { class: "seg", role: "group", "aria-label": `${group.label} view` },
    ...[["visual", "Visualization"], ["data", "Data"]].map(([v, label]) => el("button", {
      type: "button", "aria-pressed": String(v === view),
      onclick: () => { st.views[group.id] = v; section.replaceWith(groupSection(group)); },
    }, label)));
  const content = view === "visual" ? wheel(group, modifiers) : columns(modifiers);
  const section = el("section", { class: "card ex-group", id: `ex-${group.id}` },
    el("div", { class: "ex-head" },
      el("div", {},
        el("h2", { class: "ex-group-title" }, group.label, el("span", { class: "count-pill" }, total)),
        el("p", { class: "card-sub muted" }, group.blurb)),
      el("div", { class: "ex-actions" }, toggle,
        el("button", { type: "button", class: "btn btn-ghost btn-sm",
                       onclick: (e) => copyText(groupKeywords(group.id).join("\n"), e.currentTarget) }, "Copy"),
        view === "visual" ? el("button", { type: "button", class: "btn btn-ghost btn-sm",
          onclick: () => downloadImage(content.querySelector("svg"), `${slug(st.data.keyword)}-${group.id}`) }, "Image") : null,
        el("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => exportCsv(group.id) }, "CSV"))),
    body);
  body.append(content);
  return section;
}

/** PNG of a wheel. Styles come from CSS variables, so copy the computed ones onto a clone first,
 *  as SVG presentation attributes (inline style="" is blocked by the page's CSP). */
function downloadImage(source, name) {
  const clone = source.cloneNode(true);
  const props = ["fill", "stroke", "stroke-width", "opacity", "font-size", "font-weight", "font-family"];
  const from = [source, ...source.querySelectorAll("*")];
  const to = [clone, ...clone.querySelectorAll("*")];
  from.forEach((node, i) => {
    const cs = getComputedStyle(node);
    for (const p of props) to[i].setAttribute(p, cs.getPropertyValue(p));
    to[i].removeAttribute("class");
  });
  const size = 920;
  clone.setAttribute("xmlns", SVG);
  clone.setAttribute("width", size);
  clone.setAttribute("height", size);
  const bg = document.createElementNS(SVG, "rect");
  bg.setAttribute("width", "100%");
  bg.setAttribute("height", "100%");
  bg.setAttribute("fill", getComputedStyle(document.body).backgroundColor);
  clone.prepend(bg);
  const text = new XMLSerializer().serializeToString(clone);
  const img = new Image();
  img.onload = () => {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = size * 2;
    const ctx = canvas.getContext("2d");
    ctx.scale(2, 2);
    ctx.drawImage(img, 0, 0);
    try {
      canvas.toBlob((blob) => (blob ? download(`${name}.png`, "image/png", blob)
        : download(`${name}.svg`, "image/svg+xml", text)), "image/png");
    } catch {
      download(`${name}.svg`, "image/svg+xml", text); // browser refused the canvas: fall back to SVG
    }
  };
  img.onerror = () => download(`${name}.svg`, "image/svg+xml", text);
  img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(text)}`;
}

function groupKeywords(groupId) {
  return (st.data.groups[groupId] || []).flatMap((m) => m.keywords);
}

function ideaFor(keyword) {
  st.ideaIndex ||= new WeakMap();
  let index = st.ideaIndex.get(st.data);
  if (!index) {
    index = new Map(st.data.keywords.map((k) => [k.keyword, k]));
    st.ideaIndex.set(st.data, index);
  }
  return index.get(keyword) || { keyword, intent: "informational", position: 10 };
}

const rankBin = (position) => (position <= 3 ? "r1" : position <= 6 ? "r2" : "r3");

// ---------------------------------------------------------------- list (Answer Socrates style)

function columns(modifiers) {
  return el("div", { class: "kw-cards ex-cols" }, modifiers.map((m) =>
    el("div", { class: "kw-card" },
      el("div", { class: "kw-card-head" }, m.modifier, el("span", { class: "muted" }, m.keywords.length)),
      el("ul", {}, m.keywords.map((kw) => {
        const idea = ideaFor(kw);
        return el("li", {},
          el("span", { class: `idot ${idea.intent}`, title: idea.intent, "aria-label": idea.intent }),
          el("button", { type: "button", class: "kw-link", onclick: () => openMenu(idea) }, kw));
      })))));
}

// ---------------------------------------------------------------- wheel (AnswerThePublic style)

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) node.setAttribute(k, String(v));
  for (const c of children.flat()) if (c != null) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

/** Take keywords round-robin across modifiers (best-ranked first) so every spoke keeps its top ideas. */
function capLeaves(modifiers) {
  const total = modifiers.reduce((s, m) => s + m.keywords.length, 0);
  if (total <= MAX_LEAVES) return modifiers;
  const take = new Map(modifiers.map((m) => [m.modifier, 0]));
  let left = MAX_LEAVES;
  for (let round = 0; left > 0; round++) {
    let added = false;
    for (const m of modifiers) {
      if (left && m.keywords.length > round) { take.set(m.modifier, round + 1); left--; added = true; }
    }
    if (!added) break;
  }
  return modifiers.map((m) => ({ ...m, keywords: m.keywords.slice(0, take.get(m.modifier)) }));
}

function wheel(group, allModifiers) {
  const modifiers = capLeaves(allModifiers).filter((m) => m.keywords.length);
  const shown = modifiers.reduce((s, m) => s + m.keywords.length, 0);
  const total = allModifiers.reduce((s, m) => s + m.keywords.length, 0);

  const size = 920, cx = size / 2, cy = size / 2;
  const rc = 74, rm = 150, rl = 238;
  const pt = (r, a) => [cx + r * Math.sin(a), cy - r * Math.cos(a)];
  const slots = shown + modifiers.length; // one empty slot between spokes
  const step = (Math.PI * 2) / Math.max(slots, 12);
  const maxChars = 34;

  const root = svg("svg", { viewBox: `0 0 ${size} ${size}`, class: "ex-wheel", role: "img",
    "aria-label": `${group.label} for ${st.data.keyword}: ${modifiers.map((m) => `${m.modifier} (${m.keywords.length})`).join(", ")}. Switch to Data for a readable list.` });
  const links = svg("g", { class: "ex-links" });
  const leaves = svg("g");
  const hubs = svg("g");

  let slot = 0.5;
  for (const m of modifiers) {
    const first = slot;
    const angles = m.keywords.map(() => (slot++) * step);
    slot++; // gap
    const am = ((first + slot - 2) / 2) * step;
    const [mx, my] = pt(rm, am);
    links.append(svg("path", { d: `M${pt(rc, am).join(",")} L${mx},${my}`, class: "ex-spoke" }));

    m.keywords.forEach((kw, j) => {
      const a = angles[j];
      const [lx, ly] = pt(rl, a);
      const [c1x, c1y] = pt((rm + rl) / 2, am);
      const [c2x, c2y] = pt((rm + rl) / 2, a);
      links.append(svg("path", { d: `M${mx},${my} C${c1x},${c1y} ${c2x},${c2y} ${lx},${ly}`, class: "ex-link" }));
      const idea = ideaFor(kw);
      const deg = (a * 180) / Math.PI - 90;
      const left = a > Math.PI;
      const text = kw.length > maxChars ? `${kw.slice(0, maxChars - 1)}…` : kw;
      const leaf = svg("g", { class: "ex-leaf" },
        svg("title", {}, `${kw} · ${idea.intent} · autocomplete position ${idea.position}`),
        svg("circle", { cx: lx, cy: ly, r: 5, class: `ex-dot ${rankBin(idea.position)}` }),
        svg("text", {
          x: left ? cx - rl - 12 : cx + rl + 12, y: cy, class: "ex-leaf-label",
          "text-anchor": left ? "end" : "start", "dominant-baseline": "middle",
          transform: `rotate(${left ? deg + 180 : deg} ${cx} ${cy})`,
        }, text),
        // wide invisible hit target along the label
        svg("rect", {
          x: left ? cx - rl - 12 - text.length * 6.4 : cx + rl - 8, y: cy - 8,
          width: text.length * 6.4 + 20, height: 16, class: "ex-hit",
          transform: `rotate(${left ? deg + 180 : deg} ${cx} ${cy})`,
        }));
      leaf.addEventListener("click", () => openMenu(idea));
      leaves.append(leaf);
    });

    const hub = m.modifier.length > 14 ? `${m.modifier.slice(0, 13)}…` : m.modifier;
    const w = Math.max(28, hub.length * 7.4 + 16);
    hubs.append(svg("g", { class: "ex-hub" },
      svg("title", {}, `${m.modifier}: ${m.keywords.length} shown`),
      svg("rect", { x: mx - w / 2, y: my - 12, width: w, height: 24, rx: 12, class: "ex-hub-dot" }),
      svg("text", { x: mx, y: my, class: "ex-hub-label", "text-anchor": "middle", "dominant-baseline": "central" }, hub)));
  }

  root.append(links, leaves, hubs,
    svg("circle", { cx, cy, r: rc, class: "ex-center" }),
    svg("text", { x: cx, y: cy - 6, class: "ex-seed", "text-anchor": "middle" },
      st.data.keyword.length > 16 ? `${st.data.keyword.slice(0, 15)}…` : st.data.keyword),
    svg("text", { x: cx, y: cy + 16, class: "ex-seed-sub", "text-anchor": "middle" }, `${total} ideas`));

  const legend = el("div", { class: "ex-legend" },
    el("span", { class: "muted" }, "Dot shade = position in autocomplete (a popularity proxy, not search volume):"),
    el("span", {}, el("i", { class: "ex-sw r1" }), "1–3"),
    el("span", {}, el("i", { class: "ex-sw r2" }), "4–6"),
    el("span", {}, el("i", { class: "ex-sw r3" }), "7+"),
    shown < total ? el("span", { class: "muted" }, `· Showing the top ${shown} of ${total}. Switch to Data for all.`) : null);
  return el("div", { class: "ex-wheel-wrap" }, root, legend);
}

// ---------------------------------------------------------------- topics & PAA

function topicsSection() {
  const clusters = st.data.clusters.filter((c) => c.topic !== "other").slice(0, 24);
  return el("section", { class: "card", id: "ex-topics" },
    el("h2", { class: "ex-group-title" }, "Topics", el("span", { class: "count-pill" }, clusters.length)),
    el("p", { class: "card-sub muted" }, "Keywords grouped by the main word they share. Each topic can be one page or one video."),
    el("div", { class: "kw-cards" }, clusters.map((c) => el("div", { class: "kw-card" },
      el("div", { class: "kw-card-head" }, c.topic, el("span", { class: "muted" }, c.size)),
      el("ul", {}, c.keywords.slice(0, 8).map((kw) => el("li", {},
        el("span", { class: `idot ${ideaFor(kw).intent}`, "aria-hidden": "true" }),
        el("button", { type: "button", class: "kw-link", onclick: () => openMenu(ideaFor(kw)) }, kw)))),
      c.size > 8 ? el("p", { class: "muted more" }, `+${c.size - 8} more in CSV`) : null))));
}

function paaSection() {
  const { people_also_ask: paa = [], related_searches: related = [] } = st.data;
  return el("section", { class: "card", id: "ex-paa" },
    paa.length ? el("h2", { class: "ex-group-title" }, "People also ask") : null,
    paa.length ? el("ol", { class: "questions" }, paa.map((q) => el("li", {},
      safeUrl(q.link) ? el("a", { href: safeUrl(q.link), target: "_blank", rel: "noopener noreferrer" }, q.question) : q.question,
      q.snippet ? el("p", { class: "muted paa-snippet" }, q.snippet) : null))) : null,
    related.length ? el("h3", { class: paa.length ? "mt" : "" }, "Related searches") : null,
    related.length ? el("div", { class: "chips" }, related.map((r) =>
      el("button", { type: "button", class: "chip", onclick: () => exploreKeyword(r) }, r))) : null);
}

// ---------------------------------------------------------------- keyword menu

function openMenu(idea) {
  st.menuItem = idea;
  const q = encodeURIComponent(idea.keyword);
  $("ex-menu-body").replaceChildren(
    el("p", { class: "dialog-eyebrow" }, `${labelFor(idea.group)} · ${idea.modifier || ""}`),
    el("h3", {}, idea.keyword),
    el("dl", { class: "brief" },
      el("dt", {}, "Intent"), el("dd", {}, `${idea.intent}${idea.funnel ? ` (${idea.funnel})` : ""}`),
      el("dt", {}, "Autocomplete position"), el("dd", {}, `${idea.position} of 10`),
      el("dt", {}, "Look it up"), el("dd", { class: "links" },
        el("a", { href: `https://www.google.com/search?q=${q}`, target: "_blank", rel: "noopener noreferrer" }, "Google"),
        el("a", { href: `https://www.youtube.com/results?search_query=${q}`, target: "_blank", rel: "noopener noreferrer" }, "YouTube"),
        el("a", { href: `https://trends.google.com/trends/explore?geo=${st.data.country.toUpperCase()}&q=${q}`,
                  target: "_blank", rel: "noopener noreferrer" }, "Google Trends"))));
  $("ex-menu").showModal();
}

const labelFor = (id) => GROUPS.find((g) => g.id === id)?.label || id || "";

// ---------------------------------------------------------------- export

function exportCsv(groupId = null) {
  const d = st.data;
  const rows = d.keywords.filter((k) => !groupId || k.group === groupId).map((k) => ({ ...k, group: labelFor(k.group) }));
  const name = `keywords-${slug(d.keyword)}${d.region ? `-${slug(d.region)}` : ""}${groupId ? `-${groupId}` : ""}.csv`;
  download(name, "text/csv;charset=utf-8", toCsv(rows, ["keyword", "group", "modifier", "position", "intent", "funnel", "label", "brand"]));
}

async function exportAnswerSocrates() {
  const d = st.data;
  const button = $("ex-xlsx");
  button.disabled = true;
  const name = `AnswerSocrates-format-${(d.country || "").toUpperCase()}-${d.language || ""}-${slug(d.keyword)}${d.region ? `-${slug(d.region)}` : ""}-${(d.generated_at || "").slice(0, 10)}`;
  try {
    const res = await fetch("/api/keywords/xlsx", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ results: [d], name }),
    });
    if (!res.ok) throw new Error(`Export failed (${res.status})`);
    const blob = await res.blob();
    download(`${name}.xlsx`, blob.type, blob);
  } catch (err) {
    notice(err.message);
  } finally {
    button.disabled = false;
  }
}

async function copyText(text, button) {
  const original = button.textContent;
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = "Copied";
  } catch {
    button.textContent = "Copy failed";
  }
  setTimeout(() => { button.textContent = original; }, 1500);
}
