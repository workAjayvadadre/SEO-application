// Market Scraper UI. Every value from the payload is scraped, untrusted text:
// it is only ever inserted with textContent (via el()), never as HTML.

import { $, LABELS, describeError, dot, download, el, label, link, slug, toCsv } from "./common.js";
import { initExplore, routeExplore } from "./explore.js";
import { initKeywords, startPlan } from "./keywords.js";
import { OPEN_QUERIES_KEY, initCampaign, refreshOpenQueriesHint } from "./campaign.js";
import { initTrends } from "./trends.js";

const TYPE_LABELS = {
  organic_result: "Search result", people_also_ask: "People also ask", related_search: "Related search",
  post: "Post", comment: "Comment", question: "Question", answer: "Answer", video: "Video",
  hashtag_post: "Hashtag post", profile_post: "Profile post", page_post: "Page post",
  serp_discovery: "Found via search",
};
const PAGE_SIZE = 50;
const PREFS_KEY = "market-scraper:prefs";
const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });

const state = {
  platforms: [],
  selected: new Set(Object.keys(LABELS)),
  payload: null,
  filter: { platform: "all", type: "all", text: "", sort: "engagement" },
  shown: PAGE_SIZE,
  controller: null,
  timer: null,
};

function formatDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString(undefined, { dateStyle: "medium" });
}

function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    state.hadPrefs = Boolean(raw);
    const prefs = JSON.parse(raw || "{}");
    if (Array.isArray(prefs.selected)) state.selected = new Set(prefs.selected);
    if (prefs.limit) $("limit").value = prefs.limit;
    if (prefs.comments != null) $("comments").value = prefs.comments;
  } catch { /* storage unavailable: defaults are fine */ }
}

function savePrefs() {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({
      selected: [...state.selected], limit: $("limit").value, comments: $("comments").value,
    }));
  } catch { /* ignore */ }
}

function showNotice(message, isError = false) {
  const notice = $("notice");
  notice.hidden = !message;
  notice.textContent = message || "";
  notice.classList.toggle("error", isError);
}

// ---------------------------------------------------------------- platforms

async function loadPlatforms() {
  try {
    const res = await fetch("/api/platforms");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.platforms = data.platforms;
    if (!state.hadPrefs) {
      $("limit").value = data.defaults.limit;
      $("comments").value = data.defaults.comments;
    }
    const available = state.platforms.filter((p) => p.available).map((p) => p.name);
    if (!available.length) {
      showNotice("No API keys found. Add keys to .env (see .env.example) and restart the server. You can still open a results file you saved earlier.");
    }
  } catch {
    state.platforms = Object.keys(LABELS).map((name) => ({ name, available: false, reason: "Server not reachable" }));
    showNotice("Can't reach the server. Start it with: python server.py", true);
  }
  renderChips();
}

function renderChips() {
  const box = $("platform-chips");
  box.replaceChildren(...state.platforms.map((p) => {
    const on = p.available && state.selected.has(p.name);
    return el("button", {
      type: "button", class: `chip p-${p.name}`, "aria-pressed": String(on), disabled: !p.available,
      title: p.available ? null : p.reason,
      onclick: () => {
        state.selected.has(p.name) ? state.selected.delete(p.name) : state.selected.add(p.name);
        savePrefs();
        renderChips();
      },
    }, dot(p.name), label(p.name), p.available ? null : el("span", { class: "note" }, "no key"));
  }));
}

// ---------------------------------------------------------------- search

async function runSearch(event) {
  event.preventDefault();
  const query = $("query").value.trim();
  const platforms = state.platforms.filter((p) => p.available && state.selected.has(p.name)).map((p) => p.name);
  if (!query) return;
  if (!platforms.length) {
    showNotice("Pick at least one platform that has an API key.", true);
    return;
  }
  const location = $("scan-location").value.trim();
  if (location && !/^[A-Za-z][A-Za-z0-9 .,&'-]*$/.test(location)) {
    showNotice("Location can only contain letters, numbers, spaces and . , & ' -", true);
    return;
  }
  showNotice("");
  savePrefs();

  const body = {
    query, platforms,
    limit: Number($("limit").value) || 10,
    comments: Number($("comments").value) || 0,
    hashtags: $("hashtags").value.split(",").map((h) => h.trim()).filter(Boolean),
    location: $("scan-location").value.trim() || null,
    time_range: $("scan-time").value || null,
  };

  setBusy(true, platforms);
  state.controller = new AbortController();
  try {
    const res = await fetch("/api/search", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body), signal: state.controller.signal,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(describeError(err.detail) || `Server error ${res.status}`);
    }
    showReport(await res.json());
  } catch (err) {
    if (err.name !== "AbortError") showNotice(err.message, true);
  } finally {
    setBusy(false);
  }
}

function setBusy(busy, platforms = []) {
  $("run-btn").disabled = busy;
  $("cancel-btn").hidden = !busy;
  $("progress").hidden = !busy;
  clearInterval(state.timer);
  if (busy) {
    $("empty").hidden = true;
    const started = Date.now();
    const names = platforms.map(label).join(", ");
    const tick = () => {
      $("progress-text").textContent = `${names} · ${Math.round((Date.now() - started) / 1000)}s`;
    };
    tick();
    state.timer = setInterval(tick, 1000);
  } else if (!state.payload) {
    $("empty").hidden = false;
  }
}

// ---------------------------------------------------------------- report

function showReport(payload) {
  if (!payload || !Array.isArray(payload.results) || !payload.platforms) {
    showNotice("That file isn't a Market Scraper results file.", true);
    return;
  }
  state.payload = payload;
  state.filter = { platform: "all", type: "all", text: "", sort: "engagement" };
  state.shown = PAGE_SIZE;
  $("text-filter").value = "";
  $("sort").value = "engagement";
  $("empty").hidden = true;
  $("report").hidden = false;

  renderSummary(payload);
  renderTrending(payload.insights?.trending_topics || []);
  renderSentiment(payload.insights?.sentiment);
  renderQuestions(payload.insights?.audience_questions || []);
  renderPainPoints(payload.insights?.pain_points || []);
  renderOpenQueries(payload.insights?.open_queries || []);
  renderBreakdown(payload.insights?.platform_breakdown || {});
  renderResults();
  try {
    sessionStorage.setItem(OPEN_QUERIES_KEY, JSON.stringify({
      query: payload.query, questions: (payload.insights?.open_queries || []).map((q) => q.question),
    }));
  } catch { /* storage unavailable */ }
}

function renderOpenQueries(items) {
  const head = el("thead", {}, el("tr", {}, ...["Question", "Where", "Answers", "Why it's open", "Score"].map((h) => el("th", {}, h))));
  $("open-queries").replaceChildren(head, el("tbody", {}, ...(items.length ? items.slice(0, 30).map((q) => el("tr", {},
    el("td", {}, link(q.source_url, q.question)),
    el("td", { class: "nowrap" }, dot(q.platform), " ", label(q.platform)),
    el("td", {}, q.existing_answers ?? "–"), el("td", {}, q.why_open), el("td", {}, q.opportunity)))
    : [el("tr", {}, el("td", { colspan: 5, class: "none" }, "No open questions found in these results."))])));
}

function renderBreakdown(by) {
  const head = el("thead", {}, el("tr", {}, ...["Platform", "Items", "Types", "Avg engagement", "Questions", "Newest", "Recurring themes"].map((h) => el("th", {}, h))));
  $("platform-breakdown").replaceChildren(head, el("tbody", {}, ...Object.entries(by).map(([name, b]) => el("tr", {},
    el("td", { class: "nowrap" }, dot(name), " ", label(name)), el("td", {}, b.items),
    el("td", {}, Object.entries(b.types).map(([t, n]) => `${t.replaceAll("_", " ")} ${n}`).join(", ")),
    el("td", {}, b.avg_engagement), el("td", {}, b.questions), el("td", { class: "nowrap" }, b.newest ? String(b.newest).slice(0, 10) : "–"),
    el("td", {}, b.top_themes.join(", ") || "–")))));
}

function exportOpenQueries() {
  const rows = state.payload?.insights?.open_queries || [];
  download(`${fileStem()}-open-queries.csv`, "text/csv;charset=utf-8",
    toCsv(rows, ["question", "platform", "record_type", "existing_answers", "why_open", "opportunity", "engagement_score", "published_at", "source_url"]));
}

function renderSummary(p) {
  $("summary-title").textContent = `“${p.query}”`;
  const when = p.generated_at ? new Date(p.generated_at).toLocaleString() : "";
  $("summary-sub").textContent = `${p.total_results ?? p.results.length} results · ${p.duration_seconds ?? "?"}s · ${when}`;

  const entries = Object.entries(p.platforms);
  $("platform-status").replaceChildren(...entries.map(([name, info]) =>
    el("span", { class: `status p-${name} is-${info.status}` },
      dot(name), label(name), el("span", { class: "count" }, info.count),
      info.status === "ok" ? null : el("span", { class: "state" }, info.status))));

  const problems = [];
  for (const [name, info] of entries) {
    if (info.message) problems.push(el("li", {}, el("b", {}, `${label(name)}: `), info.message));
    for (const warning of info.warnings || []) problems.push(el("li", {}, el("b", {}, `${label(name)}: `), warning));
  }
  $("platform-problems").replaceChildren(...problems.slice(0, 12));
}

function renderTrending(topics) {
  const box = $("trending");
  if (!topics.length) {
    box.replaceChildren(el("p", { class: "none" }, "Not enough overlapping text to spot trends yet."));
    return;
  }
  const max = Math.max(...topics.map((t) => t.score));
  box.replaceChildren(...topics.slice(0, 15).map((t) => {
    const fill = el("div", { class: "bar-fill" });
    fill.style.width = `${Math.max(4, (t.score / max) * 100)}%`;
    return el("button", {
      type: "button", class: "bar-row", title: `Show results mentioning “${t.term}”`,
      onclick: () => filterByText(t.term),
    },
      el("span", { class: "bar-label" }, t.term),
      el("span", { class: "bar-meta" }, `${t.mentions} mentions`, ...(t.platforms || []).map(dot)),
      el("div", { class: "bar-track" }, fill));
  }));
}

function renderSentiment(sentiment) {
  const box = $("sentiment");
  const rows = [];
  const addRow = (name, s, overall = false) => {
    if (!s || !s.scored_items) return;
    const total = s.positive + s.neutral + s.negative || 1;
    const stack = el("div", {
      class: "stack", role: "img",
      "aria-label": `${s.positive} positive, ${s.neutral} neutral, ${s.negative} negative`,
    });
    for (const [cls, n] of [["seg-pos", s.positive], ["seg-neu", s.neutral], ["seg-neg", s.negative]]) {
      const seg = el("span", { class: cls });
      seg.style.width = `${(n / total) * 100}%`;
      stack.append(seg);
    }
    rows.push(el("div", { class: "sent-row" },
      el("span", { class: `sent-name${overall ? " overall" : ""}` }, overall ? null : dot(name), overall ? "Overall" : label(name)),
      stack,
      el("span", { class: "sent-avg", title: "Average sentiment, −1 to +1" },
        s.average == null ? "–" : (s.average > 0 ? "+" : "") + s.average.toFixed(2)),
      el("span", { class: "sent-counts" }, `${s.positive} pos · ${s.neutral} neu · ${s.negative} neg`)));
  };
  addRow("overall", sentiment?.overall, true);
  for (const [name, s] of Object.entries(sentiment?.by_platform || {})) addRow(name, s);
  box.replaceChildren(...(rows.length ? rows : [el("p", { class: "none" }, "No user-generated content to score.")]));
}

function renderQuestions(questions) {
  const box = $("questions");
  if (!questions.length) {
    box.replaceChildren(el("p", { class: "none" }, "No questions found."));
    return;
  }
  box.replaceChildren(...questions.slice(0, 15).map((q) =>
    el("li", {}, link(q.source_url, q.question), el("span", { class: "q-meta" }, dot(q.platform), label(q.platform)))));
}

function renderPainPoints(pains) {
  const box = $("pain-points");
  if (!pains.length) {
    box.replaceChildren(el("p", { class: "none" }, "No strongly negative content found."));
    return;
  }
  box.replaceChildren(...pains.slice(0, 8).map((p) =>
    el("div", { class: "pain" },
      el("p", {}, p.content_snippet),
      el("div", { class: "pain-meta" },
        dot(p.platform), label(p.platform), "·", TYPE_LABELS[p.record_type] || p.record_type,
        "·", `sentiment ${p.sentiment.toFixed(2)}`, "·", link(p.source_url, "Open")))));
}

// ---------------------------------------------------------------- results list

function filterByText(term) {
  state.filter.text = term;
  state.shown = PAGE_SIZE;
  $("text-filter").value = term;
  renderResults();
  $("results-section").scrollIntoView({ behavior: "smooth", block: "start" });
}

function filteredResults() {
  const { platform, type, text, sort } = state.filter;
  const needle = text.trim().toLowerCase();
  const items = state.payload.results.filter((r) =>
    (platform === "all" || r.platform === platform) &&
    (type === "all" || r.record_type === type) &&
    (!needle || `${r.title} ${r.content_snippet} ${r.author || ""}`.toLowerCase().includes(needle)));

  const time = (r) => { const t = Date.parse(r.published_at); return Number.isNaN(t) ? 0 : t; };
  const sorters = {
    engagement: (a, b) => b.engagement_score - a.engagement_score,
    newest: (a, b) => time(b) - time(a),
    negative: (a, b) => (a.sentiment ?? 2) - (b.sentiment ?? 2),
    positive: (a, b) => (b.sentiment ?? -2) - (a.sentiment ?? -2),
  };
  return items.sort(sorters[sort]);
}

function renderResults() {
  const all = state.payload.results;
  const counts = all.reduce((acc, r) => ((acc[r.platform] = (acc[r.platform] || 0) + 1), acc), {});

  const tab = (value, text, n) => el("button", {
    type: "button", class: "tab", role: "tab", "aria-selected": String(state.filter.platform === value),
    onclick: () => { state.filter.platform = value; state.filter.type = "all"; state.shown = PAGE_SIZE; renderResults(); },
  }, value === "all" ? null : dot(value), text, el("span", { class: "n" }, n));
  $("platform-tabs").replaceChildren(
    tab("all", "All", all.length),
    ...Object.keys(LABELS).filter((p) => counts[p]).map((p) => tab(p, label(p), counts[p])));

  const scoped = all.filter((r) => state.filter.platform === "all" || r.platform === state.filter.platform);
  const types = [...new Set(scoped.map((r) => r.record_type))];
  if (!types.includes(state.filter.type)) state.filter.type = "all";
  $("type-filter").replaceChildren(
    el("option", { value: "all" }, "All types"),
    ...types.map((t) => el("option", { value: t, selected: t === state.filter.type }, TYPE_LABELS[t] || t)));

  const items = filteredResults();
  $("result-count").textContent = `${items.length} of ${all.length} results`;
  $("result-list").replaceChildren(...items.slice(0, state.shown).map(resultCard));
  if (!items.length) $("result-list").replaceChildren(el("p", { class: "none" }, "Nothing matches these filters."));
  const more = $("show-more");
  more.hidden = items.length <= state.shown;
  more.textContent = `Show more (${items.length - state.shown} left)`;
}

function resultCard(r) {
  const metrics = Object.entries(r.metrics || {})
    .filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => el("span", {}, `${k.replace(/_/g, " ")} `,
      el("b", {}, typeof v === "number" ? (k.includes("ratio") || k.includes("rate") ? v : compact.format(v)) : v)));

  const snippet = r.content_snippet
    ? el("p", { class: "result-snippet", title: "Click to expand", onclick: (e) => e.currentTarget.classList.toggle("open") },
        r.content_snippet)
    : null;

  return el("article", { class: "result" },
    el("div", { class: "result-top" },
      el("span", { class: "plat" }, dot(r.platform), label(r.platform)),
      el("span", { class: "tag" }, TYPE_LABELS[r.record_type] || r.record_type),
      r.author ? el("span", {}, r.author) : null,
      formatDate(r.published_at) ? el("span", {}, formatDate(r.published_at)) : null,
      el("span", { class: "spacer" }),
      r.sentiment_label ? el("span", { class: `sent ${r.sentiment_label}`, title: `Sentiment ${r.sentiment}` }, r.sentiment_label) : null,
      el("span", { class: "eng", title: "Engagement score" }, `▲ ${r.engagement_score}`)),
    el("div", { class: "result-title" }, link(r.source_url, r.title || r.source_url)),
    snippet,
    metrics.length ? el("div", { class: "result-meta" }, metrics) : null);
}

// ---------------------------------------------------------------- export

function fileStem() {
  return `market-scan-${slug(state.payload.query)}-${(state.payload.generated_at || "").slice(0, 10)}`;
}

const RESULT_COLUMNS = ["timestamp", "platform", "record_type", "title", "content_snippet", "engagement_score",
                        "sentiment", "sentiment_label", "author", "published_at", "source_url"];

// ---------------------------------------------------------------- wiring

// ---------------------------------------------------------------- tabs

function showView() {
  const views = ["explore", "keywords", "scan", "trends", "campaign"];
  const view = { "#keywords": "keywords", "#scan": "scan", "#trends": "trends", "#campaign": "campaign" }[location.hash] || "explore";
  if (view === "campaign") refreshOpenQueriesHint();
  for (const v of views) $(`${v}-view`).hidden = v !== view;
  if (view === "explore" && !routeExplore(location.hash)) {
    history.replaceState(null, "", "#searches");
    routeExplore("#searches");
  }
  for (const a of document.querySelectorAll(".modes a")) {
    const target = view === "explore" ? "#searches" : `#${view}`;
    a.setAttribute("aria-current", a.getAttribute("href") === target ? "page" : "false");
  }
  $("file-label").hidden = view !== "scan";
}

/** Called from the keyword tab: carry a keyword over into the market scan. */
function scanKeyword(keyword) {
  location.hash = "#scan";
  $("query").value = keyword;
  $("query").focus();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function init() {
  loadPrefs();
  loadPlatforms();
  initKeywords({ onScan: scanKeyword });
  initExplore({ onScan: scanKeyword, onPlan: startPlan });
  initTrends({ onPlan: (keyword) => startPlan({ keyword, country: "in" }) });
  initCampaign();
  $("oq-csv").addEventListener("click", exportOpenQueries);
  window.addEventListener("hashchange", showView);
  showView();

  $("search-form").addEventListener("submit", runSearch);
  $("cancel-btn").addEventListener("click", () => state.controller?.abort());
  $("limit").addEventListener("change", savePrefs);
  $("comments").addEventListener("change", savePrefs);

  let debounce;
  $("text-filter").addEventListener("input", (e) => {
    clearTimeout(debounce);
    debounce = setTimeout(() => { state.filter.text = e.target.value; state.shown = PAGE_SIZE; renderResults(); }, 150);
  });
  $("type-filter").addEventListener("change", (e) => { state.filter.type = e.target.value; state.shown = PAGE_SIZE; renderResults(); });
  $("sort").addEventListener("change", (e) => { state.filter.sort = e.target.value; renderResults(); });
  $("show-more").addEventListener("click", () => { state.shown += PAGE_SIZE; renderResults(); });

  $("dl-json").addEventListener("click", () =>
    download(`${fileStem()}.json`, "application/json", JSON.stringify(state.payload, null, 2)));
  $("dl-csv").addEventListener("click", () =>
    download(`${fileStem()}.csv`, "text/csv;charset=utf-8", toCsv(state.payload.results, RESULT_COLUMNS)));

  $("file-input").addEventListener("change", async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    try {
      showNotice("");
      showReport(JSON.parse(await file.text()));
    } catch {
      showNotice("Couldn't read that file as JSON.", true);
    }
  });
}

init();
