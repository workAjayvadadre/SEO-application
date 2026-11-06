// SEO planner tab: keyword research → topic wheel → channel-wise monthly calendar.
// All keyword text comes from Google autocomplete (external), so it is only inserted as text.

import { $, describeError, download, el, slug, toCsv } from "./common.js";

const CHANNELS = [
  { id: "blog", label: "Website blog", short: "Blog", perWeek: 3 },
  { id: "instagram", label: "Instagram", short: "Instagram", perWeek: 4 },
  { id: "youtube", label: "YouTube", short: "YouTube", perWeek: 2 },
  { id: "facebook", label: "Facebook", short: "Facebook", perWeek: 2 },
  { id: "gbp", label: "Google Business Profile", short: "Google Business", perWeek: 1 },
];
const CHANNEL = Object.fromEntries(CHANNELS.map((c) => [c.id, c]));
const GROUP_LABELS = {
  questions: "Questions", past: "In the past", prepositions: "Prepositions", comparisons: "Comparisons",
  alphabetical: "Letters", query: "Query", local: "Local", youtube: "YouTube searches",
};
const INTENTS = ["informational", "commercial", "transactional", "navigational"];

const POPULAR_REGIONS = ["Delhi NCR", "Mumbai", "Bengaluru", "Hyderabad", "Chennai", "Kolkata", "Pune",
  "Ahmedabad", "Jaipur", "Lucknow", "Chandigarh", "Indore", "Patna", "Bhopal"];
const STATES = ["Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
  "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
  "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura",
  "Uttar Pradesh", "Uttarakhand", "West Bengal", "Delhi", "Jammu and Kashmir", "Ladakh", "Puducherry",
  "Chandigarh", "Andaman and Nicobar Islands", "Dadra and Nagar Haveli and Daman and Diu", "Lakshadweep"];
const MORE_CITIES = ["Surat", "Nagpur", "Kanpur", "Visakhapatnam", "Vadodara", "Ludhiana", "Agra", "Nashik",
  "Varanasi", "Udaipur", "Jodhpur", "Kota", "Ajmer", "Bikaner", "Raipur", "Ranchi", "Guwahati", "Bhubaneswar",
  "Coimbatore", "Kochi", "Madurai", "Mysuru", "Dehradun", "Noida", "Gurugram", "Ghaziabad", "Faridabad", "Thane",
  "Navi Mumbai", "Amritsar", "Jammu", "Srinagar", "Siliguri", "Vijayawada", "Thiruvananthapuram", "Mangaluru",
  "Aurangabad", "Rajkot", "Gwalior", "Jabalpur", "Prayagraj", "Meerut", "Bareilly", "Gorakhpur", "Hubballi"];
const REGION_OK = /^[A-Za-z][A-Za-z0-9 .&'-]*$/;
const PREFS_KEY = "market-scraper:planner";
const SVG = "http://www.w3.org/2000/svg";

const st = {
  step: 1,
  region: null,
  data: null,
  tab: "calendar",
  hiddenChannels: new Set(),
  kwFilter: { text: "", group: "all", intent: "all", topic: null },
  openItem: null,
  timer: null,
  onScan: () => {},
};

// ---------------------------------------------------------------- setup

export function initKeywords({ onScan }) {
  st.onScan = onScan;
  const next = new Date();
  next.setMonth(next.getMonth() + 1, 1);
  $("kw-month").value = `${next.getFullYear()}-${String(next.getMonth() + 1).padStart(2, "0")}`;

  const prefs = loadPrefs();
  if (prefs.country) $("kw-country").value = prefs.country;
  if (prefs.language) $("kw-language").value = prefs.language;
  renderChannelInputs(prefs.cadence || {});
  renderRegionChips();
  loadRegionList();

  $("kw-form").addEventListener("submit", onSubmit);
  $("kw-back").addEventListener("click", () => setStep(1));
  $("kw-country").addEventListener("change", () => { if (st.step > 1) setStep(2); });
  $("region-input").addEventListener("input", (e) => {
    if (e.target.value.trim()) { st.region = null; renderRegionChips(); }
  });

  for (const tab of $("kw-tabs").querySelectorAll(".tab")) {
    tab.addEventListener("click", () => showTab(tab.dataset.tab));
  }
  $("kw-filter").addEventListener("input", (e) => { st.kwFilter.text = e.target.value; renderKeywords(); });
  $("kw-group").addEventListener("change", (e) => { st.kwFilter.group = e.target.value; renderKeywords(); });
  $("kw-intent").addEventListener("change", (e) => { st.kwFilter.intent = e.target.value; renderKeywords(); });
  $("kw-copy").addEventListener("click", copyKeywords);
  $("kw-xlsx").addEventListener("click", exportExcel);
  $("kw-csv").addEventListener("click", exportCsv);
  $("item-copy").addEventListener("click", copyBrief);
  $("item-gen").addEventListener("click", generateBrief);
  $("item-dialog").addEventListener("close", () => { st.scriptAbort?.abort(); });
  $("item-scan").addEventListener("click", () => {
    $("item-dialog").close();
    if (st.openItem) st.onScan(st.openItem.keyword);
  });
}

/** Open the planner at step 2 with a keyword chosen elsewhere (e.g. the keyword explorer). */
export function startPlan({ keyword, country, region }) {
  location.hash = "#keywords";
  $("kw-input").value = keyword;
  if (country && [...$("kw-country").options].some((o) => o.value === country)) $("kw-country").value = country;
  const known = ["All India", ...POPULAR_REGIONS].find((r) => r.toLowerCase() === (region || "").toLowerCase());
  st.region = known && known !== "All India" ? known : null;
  $("region-input").value = region && !known ? region : "";
  setStep(2);
}

/** Fill the shared <datalist>: every state, every city (labelled with its state) and the areas
 *  inside big cities ("Dwarka Delhi", labelled "Area in Delhi"). Falls back to the short list. */
async function loadRegionList() {
  const opt = (value, label) => el("option", { value, label });
  let options = [...POPULAR_REGIONS, ...MORE_CITIES, ...STATES].map((r) => opt(r, ""));
  try {
    const res = await fetch("/static/regions.json");
    if (res.ok) {
      const data = await res.json();
      const seen = new Set();
      const add = (value, label) => {
        const key = value.toLowerCase();
        if (seen.has(key) || !REGION_OK.test(value)) return null;
        seen.add(key);
        return opt(value, label);
      };
      options = [
        ...POPULAR_REGIONS.map((r) => add(r, "Popular")),
        ...Object.entries(data.cities).flatMap(([state, cities]) => cities.map((c) => add(c, state))),
        ...data.states.map((s) => add(s, "State / UT")),
        ...Object.entries(data.areas).flatMap(([city, areas]) =>
          areas.map((a) => add(a.toLowerCase().includes(city.toLowerCase()) ? a : `${a} ${city}`, `Area in ${city}`))),
      ].filter(Boolean);
    }
  } catch { /* keep the short built-in list */ }
  $("region-list").replaceChildren(...options);
}

function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"); } catch { return {}; }
}

function savePrefs() {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({
      country: $("kw-country").value, language: $("kw-language").value, cadence: readCadence(),
    }));
  } catch { /* storage unavailable */ }
}

function renderChannelInputs(saved) {
  $("channel-grid").replaceChildren(...CHANNELS.map((c) => {
    const n = saved[c.id] ?? c.perWeek;
    return el("div", { class: `channel-row ch-${c.id}` },
      el("label", { class: "check" },
        el("input", { type: "checkbox", id: `ch-on-${c.id}`, checked: n > 0 }),
        el("span", { class: "sw-ch", "aria-hidden": "true" }), c.label),
      el("label", { class: "per-week" },
        el("input", { type: "number", id: `ch-n-${c.id}`, min: 1, max: 7, value: n > 0 ? n : c.perWeek,
                      "aria-label": `${c.label} posts per week` }),
        el("span", { class: "muted" }, "/ week")));
  }));
}

function readCadence() {
  return Object.fromEntries(CHANNELS.map((c) => {
    const on = $(`ch-on-${c.id}`).checked;
    const n = Math.max(1, Math.min(7, Number($(`ch-n-${c.id}`).value) || c.perWeek));
    return [c.id, on ? n : 0];
  }));
}

function renderRegionChips() {
  const india = $("kw-country").value === "in";
  const options = india ? ["All India", ...POPULAR_REGIONS] : [];
  $("region-chips").replaceChildren(...options.map((name) => {
    const value = name === "All India" ? null : name;
    const typed = $("region-input").value.trim();
    const checked = !typed && st.region === value;
    return el("button", {
      type: "button", class: "chip", role: "radio", "aria-checked": String(checked),
      onclick: () => { st.region = value; $("region-input").value = ""; renderRegionChips(); },
    }, name);
  }));
}

function setStep(step) {
  st.step = step;
  const later = step > 1;
  $("region-step").hidden = !later;
  $("channel-step").hidden = !later;
  $("kw-next").hidden = later;
  if (later) {
    const india = $("kw-country").value === "in";
    $("region-title").textContent = india ? "Which region in India?" : "Which city or region? (optional)";
    renderRegionChips();
    ($("region-chips").querySelector("[aria-checked='true']") || $("region-input")).focus();
  } else {
    $("kw-input").focus();
  }
}

function notice(message) {
  $("kw-notice").hidden = !message;
  $("kw-notice").textContent = message || "";
  $("kw-notice").classList.toggle("error", Boolean(message));
}

// ---------------------------------------------------------------- run

function onSubmit(event) {
  event.preventDefault();
  notice("");
  if (!$("kw-input").value.trim()) {
    notice("Enter a keyword to research.");
    $("kw-input").focus();
    return;
  }
  if (st.step === 1) {
    setStep(2);
    return;
  }
  runPlan();
}

async function runPlan() {
  const keyword = $("kw-input").value.trim();
  const region = $("region-input").value.trim() || st.region || null;
  if (region && !REGION_OK.test(region)) {
    notice("Region can only contain letters, numbers, spaces, '.', '&', ''' and '-'.");
    return;
  }
  const cadence = readCadence();
  if (!Object.values(cadence).some(Boolean)) {
    notice("Pick at least one channel.");
    return;
  }
  savePrefs();

  const body = {
    keyword, region, cadence,
    country: $("kw-country").value, language: $("kw-language").value,
    month: $("kw-month").value, include_serp: $("kw-serp").checked,
  };
  if (!/^20\d\d-\d\d$/.test(body.month)) {
    notice("Pick a month.");
    return;
  }

  setBusy(true, keyword, region);
  try {
    const res = await fetch("/api/plan", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(describeError(err.detail) || `Server error ${res.status}`);
    }
    const data = await res.json();
    data.plan.items.forEach((item, i) => { item.id = i; });
    st.data = data;
    st.hiddenChannels.clear();
    st.kwFilter = { text: "", group: "all", intent: "all", topic: null };
    $("kw-filter").value = "";
    $("kw-intent").value = "all";
    renderReport();
  } catch (err) {
    notice(err.message === "Failed to fetch" ? "Can't reach the server. Is python server.py running?" : err.message);
  } finally {
    setBusy(false);
  }
}

function setBusy(busy, keyword = "", region = null) {
  $("kw-run").disabled = busy;
  $("kw-progress").hidden = !busy;
  clearInterval(st.timer);
  if (!busy) return;
  $("kw-empty").hidden = true;
  const started = Date.now();
  const tick = () => {
    const s = Math.round((Date.now() - started) / 1000);
    $("kw-progress-text").textContent =
      ` “${keyword}”${region ? ` in ${region}` : ""} · Google + YouTube autocomplete · ${s}s (usually 15–25s)`;
  };
  tick();
  st.timer = setInterval(tick, 1000);
}

// ---------------------------------------------------------------- report

function monthName(month) {
  const [y, m] = month.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

function allKeywords() {
  const { research, youtube } = st.data;
  const google = research.keywords.map((k) => ({ ...k, source: "google" }));
  const seen = new Set(google.map((k) => k.keyword));
  const yt = (youtube?.keywords || []).filter((k) => !seen.has(k.keyword))
    .map((k) => ({ ...k, group: "youtube", source: "youtube" }));
  return [...google, ...yt];
}

function renderReport() {
  const { research, youtube, plan } = st.data;
  $("kw-report").hidden = false;
  $("kw-empty").hidden = true;
  $("kw-title").textContent = `“${research.keyword}”${research.region ? ` · ${research.region}` : ""}`;
  const totalKw = research.total_keywords + (youtube?.total_keywords || 0);
  $("kw-sub").textContent = `${totalKw} keywords · ${plan.total_items} posts planned for ${monthName(plan.month)}`
    + (research.requests.failed ? ` · ${research.requests.failed} autocomplete requests failed` : "");

  const counts = { ...research.counts, youtube: youtube?.total_keywords || 0 };
  const tiles = Object.entries(counts).filter(([, n]) => n).map(([g, n]) =>
    el("button", {
      type: "button", class: "stat", title: `Show ${GROUP_LABELS[g] || g}`,
      onclick: () => { st.kwFilter.group = g; st.kwFilter.topic = null; showTab("keywords"); },
    }, el("span", { class: "stat-n" }, n), el("span", { class: "stat-l" }, GROUP_LABELS[g] || g)));
  $("kw-stats").replaceChildren(...tiles, intentBar(research.intents));

  renderCalendar();
  renderWheel();
  renderKeywords();
  renderPaa();
  showTab(st.tab);
}

export function intentBar(intents) {
  const total = INTENTS.reduce((s, i) => s + (intents[i] || 0), 0) || 1;
  const bar = el("div", { class: "intent-bar", role: "img",
    "aria-label": INTENTS.map((i) => `${intents[i] || 0} ${i}`).join(", ") });
  const legend = el("div", { class: "intent-legend" });
  INTENTS.forEach((intent, i) => {
    const n = intents[intent] || 0;
    if (!n) return;
    const seg = el("span", { class: `seg c${i + 1}`, title: `${intent}: ${n}` });
    seg.style.flexGrow = String(n / total);
    bar.append(seg);
    legend.append(el("span", {}, el("i", { class: `sw c${i + 1}` }), `${intent} `, el("b", {}, n)));
  });
  return el("div", { class: "intent" }, el("span", { class: "stat-l" }, "Search intent"), bar, legend);
}

function showTab(tab) {
  st.tab = tab;
  for (const btn of $("kw-tabs").querySelectorAll(".tab")) {
    btn.setAttribute("aria-selected", String(btn.dataset.tab === tab));
  }
  for (const panel of document.querySelectorAll("#kw-report [data-panel]")) {
    panel.hidden = panel.dataset.panel !== tab;
  }
  if (tab === "keywords") renderKeywords();
}

// ---------------------------------------------------------------- calendar

const iso = (y, m, d) => `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;

function renderCalendar() {
  const { plan } = st.data;
  const [y, m] = plan.month.split("-").map(Number);
  $("cal-title").textContent = `Content plan · ${monthName(plan.month)}`;

  $("cal-filters").replaceChildren(...CHANNELS.filter((c) => plan.summary[c.id]).map((c) =>
    el("button", {
      type: "button", class: `chip ch-${c.id}`, "aria-pressed": String(!st.hiddenChannels.has(c.id)),
      onclick: () => {
        st.hiddenChannels.has(c.id) ? st.hiddenChannels.delete(c.id) : st.hiddenChannels.add(c.id);
        renderCalendar();
      },
    }, el("span", { class: "sw-ch", "aria-hidden": "true" }), c.label, el("span", { class: "note" }, plan.summary[c.id]))));

  const byDate = {};
  for (const item of plan.items) {
    if (!st.hiddenChannels.has(item.channel)) (byDate[item.date] ||= []).push(item);
  }

  const grid = el("div", { class: "cal-grid" });
  for (const d of ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]) grid.append(el("div", { class: "cal-dow" }, d));
  const firstDow = new Date(y, m - 1, 1).getDay();
  for (let i = 0; i < firstDow; i++) grid.append(el("div", { class: "cal-day blank", "aria-hidden": "true" }));

  const days = new Date(y, m, 0).getDate();
  for (let d = 1; d <= days; d++) {
    const date = iso(y, m, d);
    const items = byDate[date] || [];
    const weekday = new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: "short" });
    const cell = el("div", { class: `cal-day${items.length ? "" : " empty-day"}`, "data-date": date },
      el("div", { class: "cal-date" }, el("span", { class: "cal-wd" }, weekday), el("span", {}, d)),
      ...items.map(calendarCard));
    cell.addEventListener("dragover", (e) => { e.preventDefault(); cell.classList.add("drop"); });
    cell.addEventListener("dragleave", () => cell.classList.remove("drop"));
    cell.addEventListener("drop", (e) => {
      e.preventDefault();
      cell.classList.remove("drop");
      moveItem(Number(e.dataTransfer.getData("text/plain")), date);
    });
    grid.append(cell);
  }
  $("calendar").replaceChildren(grid);
  renderRegionSummary(plan);
}

function renderRegionSummary(plan) {
  const box = $("cal-region");
  const place = plan.place || {};
  if (!plan.region && !(plan.events || []).length) { box.replaceChildren(); return; }
  const where = [place.area, place.city, place.state].filter(Boolean).join(", ") || plan.region || "All India";
  const unknown = plan.region && !place.state;
  box.replaceChildren(
    el("p", {},
      el("strong", {}, where),
      plan.languages?.length ? ` · Write in ${plan.languages.join(" / ")}` : "",
      ` · ${plan.local_items || 0} cards use local searches`,
      plan.nmc_reframed ? ` · ${plan.nmc_reframed} keywords reframed for NMC compliance` : ""),
    unknown ? el("p", { class: "muted" }, `"${plan.region}" isn't in the place list, so state festivals and language could not be matched. Pick the nearest city or the state from the suggestions.`) : "",
    (plan.events || []).length ? el("ul", { class: "event-list" }, ...plan.events.map((e) =>
      el("li", { class: `ev-${e.kind}` }, el("span", { class: "ev-date" }, e.date.slice(8)), " ", e.name,
        e.verify ? el("span", { class: "muted" }, " (verify date)") : ""))) : "");
}

function calendarCard(item) {
  const c = CHANNEL[item.channel];
  const pop = el("span", { class: "pop-fill" });
  pop.style.width = `${Math.round((item.popularity - 1) * 100)}%`;
  const card = el("div", {
    class: `cal-card ch-${item.channel}`, draggable: "true", tabindex: "0", role: "button",
    "aria-label": `${c.label} ${item.format}: ${item.keyword}`,
    ondragstart: (e) => { e.dataTransfer.setData("text/plain", String(item.id)); e.dataTransfer.effectAllowed = "move"; },
    onclick: () => openItem(item),
    onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openItem(item); } },
  },
    el("div", { class: "cal-card-top" }, el("span", { class: "ch-tag" }, c.short), el("span", {}, item.format)),
    el("div", { class: "cal-kw" }, item.keyword),
    cardBadges(item),
    el("div", { class: "cal-card-foot" },
      el("span", { class: `intent-tag ${item.intent}` }, item.intent),
      el("span", { class: "pop", title: `Popularity ${item.popularity} of 2 (autocomplete rank)` }, pop)));
  return card;
}

function cardBadges(item) {
  const badges = [];
  if (item.kind === "event") badges.push(el("span", { class: `badge b-${item.event.kind}` },
    item.event.kind === "health" ? "Health day" : "Festival"));
  if (item.local) badges.push(el("span", { class: "badge b-local" }, "Local"));
  if (item.nmc?.flags?.length) badges.push(el("span", {
    class: "badge b-nmc", title: item.nmc.flags.map((f) => `${f.match}: ${f.issue} (Cl. ${f.clause})`).join("\n"),
  }, "NMC: reframed"));
  return badges.length ? el("div", { class: "badges" }, ...badges) : "";
}

function moveItem(id, date) {
  const item = st.data.plan.items.find((it) => it.id === id);
  if (!item || item.date === date) return;
  item.date = date;
  st.data.plan.items.sort((a, b) => a.date.localeCompare(b.date));
  renderCalendar();
}

function openItem(item) {
  st.openItem = item;
  const c = CHANNEL[item.channel];
  const [y, m] = st.data.plan.month.split("-").map(Number);
  const dateInput = el("input", {
    type: "date", value: item.date, min: iso(y, m, 1), max: iso(y, m, new Date(y, m, 0).getDate()),
    "aria-label": "Move to date",
    onchange: (e) => { if (e.target.value) moveItem(item.id, e.target.value); },
  });
  const row = (label, value) => [el("dt", {}, label), el("dd", {}, value)];
  $("item-body").replaceChildren(
    el("p", { class: "dialog-eyebrow" }, el("span", { class: `sw-ch ch-${item.channel}`, "aria-hidden": "true" }),
       `${c.label} · ${item.format}`),
    el("h3", {}, item.title),
    el("dl", { class: "brief" },
      row("Date", dateInput),
      row("Target keyword", item.keyword),
      row("Topic cluster", item.topic),
      row("Intent", `${item.intent} (${item.funnel})`),
      row("Popularity", `${item.popularity} of 2 · from ${item.source === "youtube" ? "YouTube" : "Google"} autocomplete rank`),
      item.hashtags?.length ? row("Hashtags", item.hashtags.join(" ")) : [],
      item.local ? row("Local", "Uses a search people make in this region") : [],
      item.kind === "event" ? row("Rule", `${item.event.rule}${item.event.verify ? ". Verify the date against the local calendar." : "."}`) : [],
      item.nmc?.flags?.length ? row("NMC check", el("div", {},
        el("p", { class: "nmc-warn" }, "The keyword invites a prohibited claim, so the title above is a compliant angle. Target the keyword for SEO; never repeat the claim."),
        el("ul", {}, ...item.nmc.flags.map((f) => el("li", {}, `"${f.match}": ${f.issue} (Cl. ${f.clause})`))))) : []));
  $("item-dialog").classList.remove("wide");
  const gen = $("item-gen");
  gen.disabled = false;
  if (item.brief) {
    renderBrief(item.brief);
    $("item-dialog").classList.add("wide");
    gen.textContent = "Regenerate brief";
  } else {
    $("item-script").replaceChildren();
    gen.textContent = "Generate brief";
  }
  $("item-dialog").showModal();
}

// ---------------------------------------------------------------- deliverable brief

async function generateBrief() {
  const item = st.openItem;
  if (!item) return;
  const gen = $("item-gen");
  const out = $("item-script");
  st.scriptAbort?.abort();
  const ctrl = new AbortController();
  st.scriptAbort = ctrl;
  gen.disabled = true;
  gen.textContent = "Planning…";
  $("item-dialog").classList.add("wide");
  out.replaceChildren(el("p", { class: "muted" },
    "Planning what this piece must achieve and contain: hooks, key messages, outline, visual direction and NMC checklist. Usually 20–60 seconds."));
  const { date, channel, channel_label, format, keyword, title, intent, funnel, hashtags, event } = item;
  try {
    const res = await fetch("/api/brief", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: ctrl.signal,
      body: JSON.stringify({
        item: { date, channel, channel_label: channel_label || "", format, keyword, title,
                intent: intent || "", funnel: funnel || "", hashtags: hashtags || [], event: event || null },
        month: st.data.plan.month, region: st.data.plan.region || null,
        country: st.data.research?.country || $("kw-country").value,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(describeError(err.detail) || `Server error ${res.status}`);
    }
    const result = await res.json();
    item.brief = result;
    if (st.openItem === item) {
      renderBrief(result);
      gen.textContent = "Regenerate brief";
    }
  } catch (err) {
    if (err.name === "AbortError") return;
    out.replaceChildren(el("p", { class: "nmc-warn" },
      err.message === "Failed to fetch" ? "Can't reach the server. Is python server.py running?" : err.message));
    gen.textContent = "Try again";
  } finally {
    gen.disabled = false;
  }
}

function renderBrief(result) {
  const c = result.content;
  const sec = (heading, ...body) => el("section", { class: "script-sec" }, el("h4", {}, heading), ...body);
  const bullets = (items) => el("ul", {}, ...items.map((x) => el("li", {}, x)));
  const v = c.visual_direction;
  const verdictClass = c.compliance.verdict === "GREEN" ? "v-green" : "v-amber";
  $("item-script").replaceChildren(
    el("div", { class: `verdict ${verdictClass}` },
      el("strong", {}, `NMC check: ${c.compliance.verdict}`), ` · Class ${c.content_class}`,
      c.compliance.notes ? el("p", {}, c.compliance.notes) : ""),
    result.auto_check.length ? el("div", { class: "nmc-warn" },
      el("strong", {}, "The automatic claim check found wording to fix:"),
      el("ul", {}, ...result.auto_check.map((h) => el("li", {}, `"${h.match}": ${h.issue} (Cl. ${h.clause}). "${h.context}"`)))) : "",
    el("div", { class: "script-actions" },
      el("button", { type: "button", class: "btn btn-ghost", onclick: (e) => copyText(briefMarkdown(c), e.currentTarget) }, "Copy brief"),
      el("button", { type: "button", class: "btn btn-ghost", onclick: () => download(
        `${slug(`${st.openItem.date}-${st.openItem.channel}-${c.title}`)}-brief.md`, "text/markdown", briefMarkdown(c)) },
        "Download .md")),
    sec("Goal", el("p", {}, el("strong", {}, c.title)), el("p", {}, c.objective),
      el("p", {}, el("strong", {}, "Audience: "), c.audience),
      el("p", {}, el("strong", {}, "Regional angle: "), c.regional_angle),
      el("p", {}, el("strong", {}, "Language: "), c.language_plan)),
    sec("Hook options", el("ol", {}, ...c.hook_options.map((h) => el("li", {}, h.english,
      h.regional ? el("div", { class: "muted" }, `${h.regional} (${h.back_translation})`) : "")))),
    el("div", { class: "two-col" },
      sec("Key messages", bullets(c.key_messages)),
      sec("Questions to answer", bullets(c.questions_to_answer))),
    sec("Outline", el("div", { class: "table-wrap" }, el("table", { class: "script-table" },
      el("thead", {}, el("tr", {}, ...["Part", "Purpose", "Points", "Length"].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...c.outline.map((o) => el("tr", {},
        el("td", {}, o.part), el("td", {}, o.purpose), el("td", {}, o.points.join(" · ")), el("td", { class: "nowrap" }, o.approx_length))))))),
    el("div", { class: "two-col" },
      sec("Must include", bullets(c.must_include)),
      sec("Avoid (NMC)", bullets(c.avoid))),
    sec("Visual direction", el("div", { class: "brief-card" },
      el("p", {}, el("strong", {}, v.canvas), ` · ${v.look_and_feel}`),
      el("p", {}, `Motifs: ${v.motifs.join(", ")}`), el("p", {}, `Avoid: ${v.exclusions.join(", ")}`),
      el("p", {}, `AI mark (if AI-made): ${v.ai_mark}`), el("p", {}, `Alt text: ${v.alt_text}`))),
    sec("Caption and CTA", el("p", {}, c.caption_direction), el("p", { class: "muted" }, c.hashtags.join(" ")),
      el("p", {}, el("strong", {}, "Allowed CTA: "), c.allowed_cta)),
    el("div", { class: "two-col" },
      c.sources_to_cite.length ? sec("Sources to cite", bullets(c.sources_to_cite)) : "",
      c.kpis.length ? sec("KPIs", bullets(c.kpis)) : ""),
    sec("Compliance checklist", el("ul", { class: "checklist" }, ...c.compliance.checklist.map((x) =>
      el("li", { class: `ck-${x.status.replace("/", "").toLowerCase()}` }, `${x.status} · ${x.check} (Cl. ${x.clause})`)))),
    c.compliance.human_approval.length ? sec("Before publishing (human sign-off)", bullets(c.compliance.human_approval)) : "",
    el("p", { class: "muted small" }, `Planned by ${result.model}. AI-assisted: verify facts and placeholders, and get compliance sign-off. Not legal advice or NMC approval.`));
}

function briefMarkdown(c) {
  const it = st.openItem;
  const v = c.visual_direction;
  const list = (items) => items.map((x) => `- ${x}`);
  return [`# Brief: ${c.title}`, "",
    `${CHANNEL[it.channel].label} · ${it.format} · ${it.date} · Keyword: ${it.keyword}`,
    `NMC: ${c.compliance.verdict} (Class ${c.content_class})`, "",
    `**Objective:** ${c.objective}`, `**Audience:** ${c.audience}`, `**Regional angle:** ${c.regional_angle}`,
    `**Language:** ${c.language_plan}`, "",
    "## Hook options", ...c.hook_options.map((h, i) => `${i + 1}. ${h.english}${h.regional ? ` / ${h.regional} (${h.back_translation})` : ""}`), "",
    "## Key messages", ...list(c.key_messages), "", "## Questions to answer", ...list(c.questions_to_answer), "",
    "## Outline", ...c.outline.map((o, i) => `${i + 1}. **${o.part}** (${o.approx_length}): ${o.purpose}. ${o.points.join("; ")}`), "",
    "## Must include", ...list(c.must_include), "", "## Avoid (NMC)", ...list(c.avoid), "",
    "## Visual direction", `- Canvas: ${v.canvas}`, `- Look: ${v.look_and_feel}`, `- Motifs: ${v.motifs.join(", ")}`,
    `- Avoid: ${v.exclusions.join(", ")}`, `- AI mark: ${v.ai_mark}`, `- Alt text: ${v.alt_text}`, "",
    "## Caption and CTA", c.caption_direction, "", c.hashtags.join(" "), "", `Allowed CTA: ${c.allowed_cta}`, "",
    "## Sources", ...list(c.sources_to_cite), "", "## KPIs", ...list(c.kpis), "",
    "## Compliance checklist", ...c.compliance.checklist.map((x) => `- ${x.status}: ${x.check} (Cl. ${x.clause})`), "",
    "## Human sign-off", ...list(c.compliance.human_approval)].join("\n");
}

async function copyBrief() {
  const it = st.openItem;
  if (!it) return;
  const text = [`${CHANNEL[it.channel].label} · ${it.format} · ${it.date}`, `Title: ${it.title}`,
    `Keyword: ${it.keyword}`, `Topic: ${it.topic}`, `Intent: ${it.intent} (${it.funnel})`,
    it.hashtags?.length ? `Hashtags: ${it.hashtags.join(" ")}` : ""].filter(Boolean).join("\n");
  await copyText(text, $("item-copy"));
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

// ---------------------------------------------------------------- topic wheel

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) node.setAttribute(k, String(v));
  for (const c of children.flat()) if (c != null) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

function wrapWords(text, max) {
  const lines = [];
  let line = "";
  for (const word of text.split(" ")) {
    if ((line + " " + word).trim().length > max && line) { lines.push(line); line = word; } else line = `${line} ${word}`.trim();
  }
  if (line) lines.push(line);
  return lines.length > 2 ? [lines[0], `${lines[1].slice(0, max - 1)}…`] : lines;
}

function renderWheel() {
  const { research } = st.data;
  const rank = new Map(research.keywords.map((k) => [k.keyword, k.position]));
  const clusters = research.clusters.filter((c) => c.topic !== "other").slice(0, 8);
  const otherCount = research.clusters.slice(clusters.length).reduce((s, c) => s + c.size, 0);
  if (!clusters.length) {
    $("wheel").replaceChildren(el("p", { class: "none" }, "Not enough keywords to form topics."));
    return;
  }

  const size = 760, cx = size / 2, cy = size / 2;
  const r0 = 82, r1 = 196, r2 = 200, r3 = 360;
  const pt = (r, a) => [cx + r * Math.sin(a), cy - r * Math.cos(a)];
  const arc = (rin, rout, a0, a1) => {
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const [x0, y0] = pt(rout, a0), [x1, y1] = pt(rout, a1), [x2, y2] = pt(rin, a1), [x3, y3] = pt(rin, a0);
    return `M${x0},${y0} A${rout},${rout} 0 ${large} 1 ${x1},${y1} L${x2},${y2} A${rin},${rin} 0 ${large} 0 ${x3},${y3} Z`;
  };

  const groups = clusters.map((c) => ({
    ...c, top: [...c.keywords].sort((a, b) => (rank.get(a) ?? 99) - (rank.get(b) ?? 99)).slice(0, 6),
  }));
  const weight = groups.reduce((s, g) => s + Math.max(g.top.length, 3), 0);
  const root = svg("svg", { viewBox: `0 0 ${size} ${size}`, class: "wheel-svg", role: "img",
    "aria-label": `Topic wheel: ${groups.map((g) => `${g.topic} (${g.size} keywords)`).join(", ")}` });

  let angle = 0;
  groups.forEach((g, i) => {
    const span = (Math.max(g.top.length, 3) / weight) * Math.PI * 2;
    const a0 = angle, a1 = angle + span;
    angle = a1;
    const slot = `c${i + 1}`;
    const topicGroup = svg("g", { class: `wheel-topic ${slot}`, tabindex: "0", role: "button",
      "aria-label": `${g.topic}: ${g.size} keywords. Show them.` },
      svg("title", {}, `${g.topic}: ${g.size} keywords`),
      svg("path", { d: arc(r0, r1, a0, a1), class: "wheel-fill" }),
      svg("path", { d: arc(r1 - 6, r1, a0, a1), class: "wheel-edge" }));
    const mid = (a0 + a1) / 2;
    const [tx, ty] = pt((r0 + r1) / 2 + 4, mid);
    const lines = wrapWords(g.topic, 14);
    const label = svg("text", { x: tx, y: ty - (lines.length - 1) * 8, class: "wheel-topic-label", "text-anchor": "middle" },
      lines.map((line, j) => svg("tspan", { x: tx, dy: j ? 16 : 0 }, line)));
    const count = svg("text", { x: tx, y: ty + lines.length * 8 + 8, class: "wheel-count", "text-anchor": "middle" },
      `${g.size}`);
    topicGroup.append(label, count);
    const open = () => { st.kwFilter = { text: "", group: "all", intent: "all", topic: g.topic }; showTab("keywords"); };
    topicGroup.addEventListener("click", open);
    topicGroup.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    root.append(topicGroup);

    const step = span / g.top.length;
    g.top.forEach((kw, j) => {
      const k0 = a0 + j * step, k1 = k0 + step, km = (k0 + k1) / 2;
      const deg = (km * 180) / Math.PI - 90;
      const left = km > Math.PI;
      const text = kw.length > 30 ? `${kw.slice(0, 29)}…` : kw;
      root.append(svg("g", { class: `wheel-kw ${slot}` },
        svg("title", {}, `${kw} (${g.topic})`),
        svg("path", { d: arc(r2, r3, k0, k1), class: "wheel-kw-fill" }),
        svg("text", {
          x: left ? cx - r2 - 10 : cx + r2 + 10, y: cy, class: "wheel-kw-label",
          "text-anchor": left ? "end" : "start", "dominant-baseline": "middle",
          transform: `rotate(${left ? deg + 180 : deg} ${cx} ${cy})`,
        }, text)));
    });
  });

  const seedLines = wrapWords(research.keyword, 16);
  root.append(svg("circle", { cx, cy, r: r0 - 6, class: "wheel-center" }),
    svg("text", { x: cx, y: cy - (seedLines.length - 1) * 9 - (research.region ? 6 : 0), class: "wheel-seed", "text-anchor": "middle" },
      seedLines.map((line, j) => svg("tspan", { x: cx, dy: j ? 18 : 0 }, line))),
    research.region ? svg("text", { x: cx, y: cy + seedLines.length * 9 + 8, class: "wheel-count", "text-anchor": "middle" }, research.region) : null);

  $("wheel").replaceChildren(root, otherCount
    ? el("p", { class: "muted wheel-note" }, `${otherCount} more keywords sit in smaller topics. See the Keywords tab.`)
    : null);
}

// ---------------------------------------------------------------- keyword lists

function filteredKeywords() {
  const { text, group, intent, topic } = st.kwFilter;
  const needle = text.trim().toLowerCase();
  const inTopic = topic ? new Set(st.data.research.clusters.find((c) => c.topic === topic)?.keywords || []) : null;
  return allKeywords().filter((k) =>
    (group === "all" || k.group === group) && (intent === "all" || k.intent === intent)
    && (!needle || k.keyword.includes(needle)) && (!inTopic || inTopic.has(k.keyword)));
}

function renderKeywords() {
  if (!st.data) return;
  const all = allKeywords();
  const groupCounts = all.reduce((acc, k) => ((acc[k.group] = (acc[k.group] || 0) + 1), acc), {});
  $("kw-group").replaceChildren(el("option", { value: "all" }, `All groups (${all.length})`),
    ...Object.keys(GROUP_LABELS).filter((g) => groupCounts[g]).map((g) =>
      el("option", { value: g, selected: st.kwFilter.group === g }, `${GROUP_LABELS[g]} (${groupCounts[g]})`)));
  $("kw-group").value = st.kwFilter.group;
  $("kw-intent").value = st.kwFilter.intent;

  const items = filteredKeywords();
  const topicNote = st.kwFilter.topic
    ? el("button", { type: "button", class: "chip", "aria-pressed": "true", title: "Clear topic filter",
        onclick: () => { st.kwFilter.topic = null; renderKeywords(); } }, `Topic: ${st.kwFilter.topic} ✕`)
    : null;
  $("kw-count").replaceChildren(`${items.length} keywords `, topicNote);

  const sections = [];
  for (const group of Object.keys(GROUP_LABELS)) {
    const inGroup = items.filter((k) => k.group === group);
    if (!inGroup.length) continue;
    const byModifier = new Map();
    for (const k of inGroup) {
      const key = group === "youtube" ? k.modifier : k.modifier;
      if (!byModifier.has(key)) byModifier.set(key, []);
      byModifier.get(key).push(k);
    }
    sections.push(el("section", { class: "kw-section" },
      el("h4", {}, GROUP_LABELS[group], el("span", { class: "muted" }, ` ${inGroup.length}`)),
      el("div", { class: "kw-cards" }, [...byModifier].map(([modifier, list]) =>
        el("div", { class: "kw-card" },
          el("div", { class: "kw-card-head" }, modifier, el("span", { class: "muted" }, list.length)),
          el("ul", {}, list.map((k) => el("li", {},
            el("span", { class: `idot ${k.intent}`, title: k.intent, "aria-label": k.intent }),
            el("span", { class: "kw-text" }, k.keyword),
            el("button", { type: "button", class: "kw-scan", title: `Market-scan “${k.keyword}”`,
                           onclick: () => st.onScan(k.keyword) }, "Scan")))))))));
  }
  $("kw-groups").replaceChildren(...(sections.length ? sections : [el("p", { class: "none" }, "No keywords match.")]));
}

async function copyKeywords() {
  await copyText(filteredKeywords().map((k) => k.keyword).join("\n"), $("kw-copy"));
}

function renderPaa() {
  const { research } = st.data;
  const paa = research.people_also_ask || [];
  const related = research.related_searches || [];
  if (!paa.length && !related.length) {
    $("paa").replaceChildren(el("p", { class: "none" },
      "Tick “Add Google People also ask” in step 3 to include these (uses 1 Serper credit)."));
    return;
  }
  const safe = (u) => { try { const x = new URL(u); return /^https?:$/.test(x.protocol) ? x.href : null; } catch { return null; } };
  $("paa").replaceChildren(
    el("h3", {}, "People also ask"),
    el("ol", { class: "questions" }, paa.map((q) => el("li", {},
      safe(q.link) ? el("a", { href: safe(q.link), target: "_blank", rel: "noopener noreferrer" }, q.question) : q.question,
      q.snippet ? el("p", { class: "muted paa-snippet" }, q.snippet) : null))),
    related.length ? el("h3", { class: "mt" }, "Related searches") : null,
    related.length ? el("div", { class: "chips" }, related.map((r) =>
      el("button", { type: "button", class: "chip", onclick: () => st.onScan(r) }, r))) : null);
}

// ---------------------------------------------------------------- export

async function exportExcel() {
  const button = $("kw-xlsx");
  button.disabled = true;
  try {
    const { plan, research } = st.data;
    const res = await fetch("/api/plan/xlsx", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan, keywords: allKeywords(), clusters: research.clusters }),
    });
    if (!res.ok) throw new Error(`Export failed (${res.status})`);
    const blob = await res.blob();
    download(`content-plan-${slug(research.keyword)}-${plan.month}.xlsx`, blob.type, blob);
  } catch (err) {
    notice(err.message);
  } finally {
    button.disabled = false;
  }
}

function exportCsv() {
  const { plan, research } = st.data;
  const rows = plan.items.map((it) => ({ ...it, hashtags: (it.hashtags || []).join(" ") }));
  download(`content-plan-${slug(research.keyword)}-${plan.month}.csv`, "text/csv;charset=utf-8",
    toCsv(rows, ["date", "channel_label", "format", "title", "keyword", "topic", "intent", "funnel", "popularity", "hashtags"]));
}
