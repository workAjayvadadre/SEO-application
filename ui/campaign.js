// Indira campaigns > Campaign planner: territory + channel + month -> regional research -> calendar with briefs.

import { $, describeError, download, el, link, slug, toCsv } from "./common.js";
import { studioKeywords } from "./keywords.js";

const SOCIAL_KEY = "market-scraper:social-insights";
const PREFS_KEY = "market-scraper:campaign";
const CHANNELS = [
  { id: "instagram", label: "Instagram", perWeek: 4 },
  { id: "youtube", label: "YouTube", perWeek: 2 },
  { id: "facebook", label: "Facebook", perWeek: 3 },
  { id: "blog", label: "Website blog", perWeek: 2 },
  { id: "gbp", label: "Google Business Profile", perWeek: 2 },
  { id: "google_ads", label: "Google Ads (Search)", perWeek: 1 },
];
const IMPACT_ORDER = { high: 0, medium: 1, low: 2 };
const st = { geo: null, busy: false, regional: null, result: null, ctrl: null };

export function initCampaign() {
  $("cp-channel").replaceChildren(...CHANNELS.map((c) => el("option", { value: c.id }, c.label)));
  const next = new Date();
  next.setMonth(next.getMonth() + 1);
  $("cp-month").value = `${next.getFullYear()}-${String(next.getMonth() + 1).padStart(2, "0")}`;
  $("cp-channel").addEventListener("change", () => {
    // Google Ads plans one ad group per week, so posts-per-week doesn't apply.
    const c = CHANNELS.find((x) => x.id === $("cp-channel").value);
    $("cp-per-week").value = c.perWeek;
    $("cp-per-week-row").hidden = c.id === "google_ads";
  });
  for (const id of ["cp-zone", "cp-state", "cp-district"]) $(id).addEventListener("change", () => cascade(id));
  for (const id of ["cp-sub", "cp-village"]) $(id).addEventListener("input", updateSummary);
  $("cp-form").addEventListener("submit", (e) => { e.preventDefault(); void run(); });
  $("cp-use-studio").addEventListener("change", refreshSources);
  $("cp-cancel").addEventListener("click", () => st.ctrl?.abort());
  $("cp-csv").addEventListener("click", exportCsv);
  $("cp-md").addEventListener("click", exportMarkdown);
  void loadGeo();
}

async function loadGeo() {
  try {
    st.geo = await (await fetch("/static/geo.json")).json();
  } catch {
    note("Couldn't load the territory list. Reload the page.");
    return;
  }
  $("cp-zone").replaceChildren(el("option", { value: "" }, "All India"),
    ...Object.keys(st.geo.zones).map((z) => el("option", { value: z }, z)));
  const prefs = loadPrefs();
  if (prefs.channel) $("cp-channel").value = prefs.channel;
  $("cp-channel").dispatchEvent(new Event("change"));
  if (prefs.perWeek) $("cp-per-week").value = prefs.perWeek;
  // Restore the last territory level by level; each cascade fills the next list.
  const t = prefs.territory || {};
  $("cp-zone").value = t.zone || "";
  cascade("cp-zone");
  if (t.state) $("cp-state").value = t.state;
  cascade("cp-state");
  if (t.district) $("cp-district").value = t.district;
  cascade("cp-district");
  if (t.subdistrict) $("cp-sub").value = t.subdistrict;
  if (t.district && t.village) $("cp-village").value = t.village;
  updateSummary();
}

function cascade(changed) {
  const g = st.geo;
  if (!g) return;
  const zone = $("cp-zone").value;
  const fill = (id, placeholder, items) => {
    const keep = $(id).value;
    $(id).replaceChildren(el("option", { value: "" }, placeholder), ...items.map((x) => el("option", { value: x }, x)));
    $(id).disabled = !items.length;
    if (items.includes(keep)) $(id).value = keep;
  };
  if (!changed || changed === "cp-zone") {
    const states = zone ? g.zones[zone] : Object.keys(g.states);
    fill("cp-state", zone ? `All of ${zone}` : "All states", states);
  }
  if (!changed || changed === "cp-zone" || changed === "cp-state") {
    fill("cp-district", "All districts", $("cp-state").value ? g.districts[$("cp-state").value] || [] : []);
  }
  fill("cp-sub", "All tehsils / talukas", $("cp-district").value
    ? g.subdistricts[`${$("cp-state").value}|${$("cp-district").value}`] || [] : []);
  $("cp-village").disabled = !$("cp-district").value;
  if (!$("cp-district").value) $("cp-village").value = "";
  updateSummary();
}

function territory() {
  const v = (id) => $(id).value.trim() || null;
  return { zone: v("cp-zone"), state: v("cp-state"), district: v("cp-district"), subdistrict: v("cp-sub"), village: v("cp-village") };
}

function territoryLabel(t) {
  const parts = [t.village, t.subdistrict, t.district, t.state].filter(Boolean);
  return parts.length ? parts.join(", ") : t.zone || "All India";
}

function updateSummary() {
  $("cp-where").textContent = territoryLabel(territory());
  refreshSources();
}

function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"); } catch { return {}; }
}

function savePrefs() {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({ territory: territory(), channel: $("cp-channel").value,
                                                     perWeek: $("cp-per-week").value }));
  } catch { /* storage unavailable */ }
}

// ---------------------------------------------------------------- data sources

/** Called after every social listening run: keep what the planner needs, in localStorage so it survives a new tab. */
export function saveSocialInsights(payload) {
  const ins = payload?.insights || {};
  const overall = ins.sentiment?.overall || {};
  const text = (s, n) => String(s || "").trim().slice(0, n);
  const saved = {
    query: text(payload?.query, 200), run_at: text(payload?.generated_at || new Date().toISOString(), 40),
    open_queries: (ins.open_queries || []).map((q) => text(q.question, 300)).filter(Boolean).slice(0, 60),
    pain_points: (ins.pain_points || []).map((p) => text(p.content_snippet || p.title, 300)).filter(Boolean).slice(0, 20),
    trending_topics: (ins.trending_topics || []).map((t) => text(t.term, 60)).filter(Boolean).slice(0, 20),
    sentiment: { average: typeof overall.average === "number" ? Math.max(-1, Math.min(1, overall.average)) : null,
                 positive: overall.positive || 0, neutral: overall.neutral || 0, negative: overall.negative || 0 },
  };
  if (!saved.query) return;
  try { localStorage.setItem(SOCIAL_KEY, JSON.stringify(saved)); } catch { /* storage unavailable */ }
}

function socialInsights() {
  try {
    const s = JSON.parse(localStorage.getItem(SOCIAL_KEY) || "null");
    return s?.query && (s.open_queries?.length || s.pain_points?.length || s.trending_topics?.length) ? s : null;
  } catch { return null; }
}

function ago(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms)) return "date unknown";
  const mins = Math.round(ms / 60000);
  if (mins < 60) return mins <= 1 ? "just now" : `${mins} minutes ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

// The Studio search was for one free-text place; warn when it isn't part of the campaign territory.
function studioMismatch(studio, t) {
  const places = [t.village, t.subdistrict, t.district, t.state].filter(Boolean).map((p) => p.toLowerCase());
  if (!studio.region) return places.length ? `The Keyword Studio search was for all of India, not ${territoryLabel(t)}.` : "";
  return places.includes(studio.region.toLowerCase()) ? ""
    : `The Keyword Studio search was for ${studio.region}, not ${territoryLabel(t)}. Untick it to search this territory instead.`;
}

export function refreshSources() {
  const studio = studioKeywords();
  $("cp-use-studio-row").hidden = !studio;
  $("cp-studio-off").hidden = !!studio;
  const warn = studio ? studioMismatch(studio, territory()) : "";
  $("cp-studio-warn").textContent = warn;
  $("cp-studio-warn").hidden = !warn || !$("cp-use-studio").checked;
  if (studio) {
    $("cp-studio-hint").textContent = `${studio.keywords.length} keywords for “${studio.seed}”${studio.region ? ` in ${studio.region}` : ""}, ${ago(studio.run_at)}`;
  }
  const s = socialInsights();
  $("cp-use-social-row").hidden = !s;
  $("cp-social-off").hidden = !!s;
  if (s) {
    $("cp-social-hint").textContent = `from “${s.query}” run, ${ago(s.run_at)} · ${s.open_queries.length} questions, ${s.pain_points.length} pain points, ${s.trending_topics.length} topics`;
  }
}

function note(text) {
  $("cp-notice").textContent = text;
  $("cp-notice").hidden = !text;
}

async function post(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify(body), signal: st.ctrl.signal });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(describeError(data.detail) || `Server error ${res.status}`);
  return data;
}

async function run() {
  if (st.busy) return;
  const t = territory();
  if (t.village && !/^[A-Za-z][A-Za-z0-9 .,&'()/-]*$/.test(t.village)) {
    note("Village names can only contain letters, numbers, spaces and . , & ' ( ) / -");
    return;
  }
  savePrefs();
  note("");
  st.busy = true;
  st.ctrl = new AbortController();
  const channel = $("cp-channel").value;
  const base = { territory: t, month: $("cp-month").value, channels: [channel] };
  busy(true, "Researching the territory on the web (festivals, language, season, local context)… usually 1–2 minutes.");
  try {
    st.regional = await post("/api/regional", base);
    renderRegional(st.regional);
    busy(true, "Building the calendar and a deliverable brief for every slot… usually 1–3 minutes.");
    const studio = $("cp-use-studio").checked ? studioKeywords() : null;
    const social = $("cp-use-social").checked ? socialInsights() : null;
    st.result = await post("/api/campaign", {
      ...base, channel, per_week: Number($("cp-per-week").value) || 3,
      topic: $("cp-topic").value.trim() || "IVF and fertility care", seed: $("cp-seed").value.trim() || "ivf",
      keywords: studio ? studio.keywords.slice(0, 300) : [],
      social: social ? {
        query: social.query, run_at: social.run_at, sentiment: social.sentiment,
        open_queries: social.open_queries.slice(0, 60), pain_points: social.pain_points.slice(0, 20),
        trending_topics: social.trending_topics.slice(0, 20),
      } : null,
    });
    renderCampaign(st.result);
  } catch (err) {
    if (err.name !== "AbortError") {
      note(err.message === "Failed to fetch" ? "Can't reach the server. Is python server.py running?" : err.message);
    }
  } finally {
    st.busy = false;
    busy(false);
  }
}

function busy(on, text = "") {
  $("cp-run").disabled = on;
  $("cp-progress").hidden = !on;
  $("cp-progress-text").textContent = text;
}

// ---------------------------------------------------------------- regional research

function renderRegional(r) {
  const b = r.brief;
  $("cp-regional").hidden = false;
  $("cp-reg-title").textContent = `${r.territory.label} · ${monthName(r.month)}`;
  $("cp-reg-sub").textContent = `${r.cached ? "From cache (researched in the last 14 days)" : "Researched just now"} · ${r.usage?.web_searches ?? 0} web searches · ${r.sources.length} sources`;
  const events = [...b.events].sort((a, z) => (IMPACT_ORDER[a.impact] - IMPACT_ORDER[z.impact]) || a.date.localeCompare(z.date));
  const bullets = (items) => items.length ? el("ul", {}, ...items.map((x) => el("li", {}, x))) : el("p", { class: "none" }, "Nothing specific found.");
  $("cp-reg-body").replaceChildren(
    el("p", {}, b.territory_summary),
    el("p", {}, el("strong", {}, "Language: "), `${b.languages.primary}${b.languages.others.length ? ` (also ${b.languages.others.join(", ")})` : ""} · ${b.languages.script}. ${b.languages.tone_notes}`),
    el("h4", {}, "Events and moments this month"),
    el("div", { class: "table-wrap" }, el("table", { class: "data-table" },
      el("thead", {}, el("tr", {}, ...["Date", "Event", "Impact", "Where", "Why it matters", "Content angle", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...events.map((e) => el("tr", {},
        el("td", { class: "nowrap" }, e.date), el("td", {}, e.name, el("div", { class: "muted small" }, e.type.replace(/_/g, " "))),
        el("td", {}, el("span", { class: `badge imp-${e.impact}` }, e.impact)), el("td", {}, e.where),
        el("td", {}, e.why_it_matters), el("td", {}, e.content_angle),
        el("td", {}, e.verify ? el("span", { class: "badge b-nmc" }, "verify date") : "", " ", e.source ? link(e.source, "source") : "")))))),
    el("div", { class: "two-col" },
      el("div", {}, el("h4", {}, "Season"), bullets(b.seasonal_context)),
      el("div", {}, el("h4", {}, "Cultural nuances"), bullets(b.cultural_nuances)),
      el("div", {}, el("h4", {}, "Audience insights"), bullets(b.audience_insights)),
      el("div", {}, el("h4", {}, "Channel notes"), bullets(b.channel_notes)),
      el("div", {}, el("h4", {}, "Health context"), b.health_context.length
        ? el("ul", {}, ...b.health_context.map((h) => el("li", {}, h.point, " ", h.source ? link(h.source, "source") : "")))
        : el("p", { class: "none" }, "Nothing specific found.")),
      el("div", {}, el("h4", {}, "Sensitivities"), bullets(b.sensitivities))),
    b.local_search_terms.length ? el("p", {}, el("strong", {}, "Local search terms: "), b.local_search_terms.join(" · ")) : "",
    el("details", {}, el("summary", {}, `Sources (${r.sources.length})`),
      el("ol", { class: "sources" }, ...r.sources.map((s) => el("li", {}, link(s.url, s.title || s.url))))));
}

// ---------------------------------------------------------------- campaign

function renderCampaign(r) {
  const p = r.plan;
  $("cp-result").hidden = false;
  $("cp-title").textContent = `${r.channel_label} · ${r.territory.label} · ${monthName(r.month)}`;
  $("cp-sub").textContent = `${p.items.length} slots${p.ad_groups.length ? ` · ${p.ad_groups.length} ad groups` : ""} · built from keyword research, Google Trends, social listening and regional research`;
  $("cp-data-used").textContent = dataUsedLine(r.data_used);
  const s = p.strategy;
  const checks = [...r.auto_check.map((h) => `“${h.match}”: ${h.issue} (Cl. ${h.clause}). ${h.context}`),
                  ...r.ads_check.map((a) => `${a.where}: ${a.issue}: “${a.text}”`),
                  ...(r.source_check || []).map((c) => c.where === "Plan" ? c.issue : `${c.where}: ${c.issue}${c.text ? `: “${c.text}”` : ""}`)];
  $("cp-strategy").replaceChildren(
    checks.length ? el("div", { class: "nmc-warn" }, el("strong", {}, "Fix before using:"), el("ul", {}, ...checks.map((c) => el("li", {}, c)))) : "",
    el("dl", { class: "brief" },
      ...[["Objective", s.objective], ["Audience", s.audience], ["Regional hook", s.regional_hook],
          ["Language", s.language_plan], ["Tone", s.tone], ["Key themes", s.key_themes.join(" · ")],
          ["Posting", s.posting_guidance], ["Measure", s.measurement.join(" · ")]]
        .flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])));

  $("cp-items").replaceChildren(...p.items.map((it, i) => slotCard(it, i)));
  $("cp-ads").hidden = !p.ad_groups.length;
  if (p.ad_groups.length) {
    $("cp-ads-body").replaceChildren(...p.ad_groups.map((g) => el("div", { class: "brief-card" },
      el("h4", {}, `Week ${g.week}: ${g.name}`), el("p", {}, g.theme),
      el("p", {}, el("strong", {}, "Keywords: "), g.keywords.join(", ")),
      el("p", {}, el("strong", {}, "Negative keywords: "), g.negative_keywords.join(", ")),
      el("p", {}, el("strong", {}, "Geo: "), g.geo_targets.join(", "), " · ", el("strong", {}, "Schedule: "), g.schedule),
      el("p", {}, el("strong", {}, "Headlines:")),
      el("ul", { class: "ad-copy" }, ...g.headlines.map((h) => el("li", { class: h.length > 30 ? "too-long" : "" }, h, el("span", { class: "muted small" }, ` ${h.length}/30`)))),
      el("p", {}, el("strong", {}, "Descriptions:")),
      el("ul", { class: "ad-copy" }, ...g.descriptions.map((d) => el("li", { class: d.length > 90 ? "too-long" : "" }, d, el("span", { class: "muted small" }, ` ${d.length}/90`)))),
      el("p", {}, el("strong", {}, "Landing page: "), g.landing_page_brief),
      builtFromLine(g.built_from),
      el("p", { class: "muted" }, g.nmc_notes))));
  }
}

function dataUsedLine(d) {
  if (!d) return "";
  const parts = [
    `Data used: ${d.keywords} keywords (${d.keyword_origin === "studio" ? "Keyword Studio" : "automatic search"})`,
    d.trends ? `Google Trends for ${d.trends} keywords${d.trends_geo ? ` (${d.trends_geo})` : ""}` : `no Google Trends (${d.trends_error || "no data"})`,
    d.social_run ? `social listening “${d.social_run}”: ${d.social_questions} questions, ${d.pain_points} pain points, ${d.trending_topics} topics`
      : "no social listening",
  ];
  return parts.join(" · ");
}

function builtFrom(b) {
  if (!b) return "";
  return [b.keyword_source && `keyword “${b.keyword_source}”`, b.trend_signal && `trend: ${b.trend_signal}`,
          b.social_insight && `social: ${b.social_insight}`].filter(Boolean).join(" · ");
}

function builtFromLine(b) {
  const text = builtFrom(b);
  return el("p", { class: "muted small" }, el("strong", {}, "Built from: "), text || "no keyword, trend or social data (greeting or unsupported slot)");
}

function slotCard(it, i) {
  const d = it.deliverable;
  const list = (title, items) => items?.length ? [el("h5", {}, title), el("ul", {}, ...items.map((x) => el("li", {}, x)))] : [];
  return el("details", { class: "slot", open: i < 2 },
    el("summary", {},
      el("span", { class: "slot-date" }, fmtDate(it.date)),
      el("span", { class: "slot-format" }, it.format),
      el("span", { class: "slot-title" }, it.title),
      el("span", { class: `badge cls-${it.nmc_class}` }, `Class ${it.nmc_class}`)),
    el("div", { class: "slot-body" },
      el("p", {}, el("strong", {}, "Why now: "), it.why_now),
      el("p", {}, el("strong", {}, "Regional hook: "), it.regional_hook),
      el("p", {}, el("strong", {}, "Target keyword: "), it.target_keyword || "none", " · ", el("strong", {}, "Language: "), it.language),
      builtFromLine(it.built_from),
      el("p", {}, el("strong", {}, "Objective: "), d.objective),
      el("div", { class: "two-col" },
        el("div", {}, ...list("Key messages", d.key_messages), ...list("Questions to answer", d.questions_to_answer)),
        el("div", {}, ...list("Outline", d.outline), ...list("Must include", d.must_include))),
      el("div", { class: "two-col" },
        el("div", {}, ...list("Avoid (NMC)", d.avoid)),
        el("div", {}, el("h5", {}, "Visual direction"), el("p", {}, d.visual_direction),
          el("h5", {}, "Allowed CTA"), el("p", {}, d.allowed_cta))),
      el("div", { class: "two-col" },
        el("div", {}, ...list("Evidence from research", d.evidence)),
        el("div", {}, ...list("KPIs", d.kpis)))));
}

// ---------------------------------------------------------------- export

function exportCsv() {
  const r = st.result;
  if (!r) return;
  const rows = r.plan.items.map((it) => ({
    date: it.date, format: it.format, title: it.title, theme: it.theme, nmc_class: it.nmc_class,
    target_keyword: it.target_keyword, language: it.language, why_now: it.why_now, regional_hook: it.regional_hook,
    objective: it.deliverable.objective, key_messages: it.deliverable.key_messages.join(" | "),
    questions_to_answer: it.deliverable.questions_to_answer.join(" | "), outline: it.deliverable.outline.join(" | "),
    must_include: it.deliverable.must_include.join(" | "), avoid: it.deliverable.avoid.join(" | "),
    visual_direction: it.deliverable.visual_direction, allowed_cta: it.deliverable.allowed_cta,
    kpis: it.deliverable.kpis.join(" | "),
    built_from_keyword: it.built_from?.keyword_source || "", built_from_trend: it.built_from?.trend_signal || "",
    built_from_social: it.built_from?.social_insight || "",
  }));
  download(`${fileStem(r)}.csv`, "text/csv;charset=utf-8", toCsv(rows, Object.keys(rows[0] || { date: 1 })));
}

function exportMarkdown() {
  const r = st.result;
  if (!r) return;
  const p = r.plan, s = p.strategy, b = st.regional?.brief;
  const L = [`# ${r.channel_label} campaign · ${r.territory.label} · ${monthName(r.month)}`, "",
    `**Objective:** ${s.objective}`, `**Audience:** ${s.audience}`, `**Regional hook:** ${s.regional_hook}`,
    `**Language:** ${s.language_plan}`, `**Tone:** ${s.tone}`, `**Key themes:** ${s.key_themes.join(", ")}`, "",
    dataUsedLine(r.data_used), ""];
  if (b) {
    L.push("## Regional calendar", "", "| Date | Event | Impact | Content angle |", "|---|---|---|---|",
      ...b.events.map((e) => `| ${e.date}${e.verify ? " (verify)" : ""} | ${e.name} | ${e.impact} | ${e.content_angle.replace(/\|/g, "/")} |`), "");
  }
  L.push("## Slots", "");
  for (const it of p.items) {
    const d = it.deliverable;
    L.push(`### ${it.date} · ${it.format} · ${it.title} (Class ${it.nmc_class})`, "",
      `- Why now: ${it.why_now}`, `- Regional hook: ${it.regional_hook}`, `- Target keyword: ${it.target_keyword || "none"}`,
      `- Built from: ${builtFrom(it.built_from) || "none"}`,
      `- Language: ${it.language}`, `- Objective: ${d.objective}`, "",
      "**Key messages**", ...d.key_messages.map((x) => `- ${x}`), "", "**Questions to answer**", ...d.questions_to_answer.map((x) => `- ${x}`), "",
      "**Outline**", ...d.outline.map((x, i) => `${i + 1}. ${x}`), "", "**Must include**", ...d.must_include.map((x) => `- ${x}`), "",
      "**Avoid (NMC)**", ...d.avoid.map((x) => `- ${x}`), "", `**Visual direction:** ${d.visual_direction}`, `**Allowed CTA:** ${d.allowed_cta}`, "",
      "**Evidence**", ...d.evidence.map((x) => `- ${x}`), "", `**KPIs:** ${d.kpis.join(", ")}`, "");
  }
  for (const g of p.ad_groups) {
    L.push(`## Week ${g.week} ad group: ${g.name}`, "", `Keywords: ${g.keywords.join(", ")}`, `Negatives: ${g.negative_keywords.join(", ")}`,
      `Geo: ${g.geo_targets.join(", ")} · Schedule: ${g.schedule}`, `Built from: ${builtFrom(g.built_from) || "none"}`, "", "Headlines:", ...g.headlines.map((h) => `- ${h}`), "",
      "Descriptions:", ...g.descriptions.map((x) => `- ${x}`), "", `Landing page: ${g.landing_page_brief}`, "");
  }
  L.push("---", "AI-assisted plan. Verify lunar dates, statistics and any [PLACEHOLDER] before use; get compliance sign-off. Not legal advice or NMC approval.");
  download(`${fileStem(r)}.md`, "text/markdown", L.join("\n"));
}

const fileStem = (r) => `campaign-${slug(r.channel)}-${slug(r.territory.label)}-${r.month}`;
const monthName = (m) => new Date(`${m}-01T00:00:00`).toLocaleDateString(undefined, { month: "long", year: "numeric" });
const fmtDate = (d) => new Date(`${d}T00:00:00`).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
