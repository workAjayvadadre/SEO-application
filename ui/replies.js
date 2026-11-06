// Indira campaigns > Reply queue (ORM): draft -> approve -> post via API, or copy & post by hand.
// Every value shown here is scraped or user/model text: inserted with textContent only (el()).

import { $, describeError, dot, el, label, link } from "./common.js";

const REVIEWER_KEY = "market-scraper:reviewer";
const STATUS_LABEL = { draft: "Draft", approved: "Approved", posting: "Posting…", posted: "Posted", failed: "Failed" };
const st = { data: null, filter: "open", busy: new Set() };

export function initReplies() {
  $("rq-refresh").addEventListener("click", load);
  const reviewer = $("rq-reviewer");
  try { reviewer.value = localStorage.getItem(REVIEWER_KEY) || ""; } catch { /* storage blocked */ }
  reviewer.addEventListener("change", () => {
    try { localStorage.setItem(REVIEWER_KEY, reviewer.value.trim()); } catch { /* storage blocked */ }
  });
  for (const tab of document.querySelectorAll("#rq-tabs [data-filter]")) {
    tab.addEventListener("click", () => { st.filter = tab.dataset.filter; render(); });
  }
}

export function showReplies() { load(); }

/** From an open query: add it to the queue (with any answers we have as context) and open the queue. */
export async function addToReplyQueue(q, reply = null) {
  const context = [...(q.answers || []), ...(q.manual_answers || [])].map((a) => a.text).filter(Boolean).slice(0, 10);
  const res = await fetch("/api/replies", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question: q.question, platform: q.platform, record_type: q.record_type || "question",
      source_url: q.source_url, graph_id: q.graph_id || null, context, reply }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(describeError(data.detail) || `HTTP ${res.status}`);
  st.highlight = data.id;
  location.hash = "#replies";
}

function reviewer() { return $("rq-reviewer").value.trim(); }

function note(message, isError = false) {
  const box = $("rq-notice");
  box.hidden = !message;
  box.textContent = message || "";
  box.classList.toggle("error", isError);
}

async function api(path, method = "GET", body = undefined) {
  const res = await fetch(path, {
    method, headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(describeError(data.detail) || `HTTP ${res.status}`);
  return data;
}

async function load() {
  try {
    st.data = await api("/api/replies");
    note("");
    render();
  } catch (err) {
    note(`Couldn't load the reply queue: ${err.message}`, true);
  }
}

function replaceItem(item) {
  const i = st.data.items.findIndex((x) => x.id === item.id);
  if (i >= 0) st.data.items[i] = item;
  render();
}

async function act(item, fn, busyText) {
  if (st.busy.has(item.id)) return;
  st.busy.add(item.id);
  const status = document.querySelector(`[data-status-for="${item.id}"]`);
  if (status) status.textContent = busyText;
  try {
    const updated = await fn();
    if (updated) replaceItem(updated); else await load();
  } catch (err) {
    if (status) status.textContent = err.message;
  } finally {
    st.busy.delete(item.id);
  }
}

function setupBanner(d) {
  const lines = [];
  if (!d.drafting) lines.push("ANTHROPIC_API_KEY is not set: write replies by hand, or add the key to draft with Claude.");
  const chips = Object.entries(d.capabilities).map(([p, c]) =>
    el("span", { class: `rq-cap ${c.auto ? "on" : "off"}`, title: c.reason || "Approved replies can be posted from here" },
      dot(p), label(p), c.auto ? " · auto-post" : " · copy & post by hand"));
  $("rq-setup").replaceChildren(
    el("div", { class: "rq-caps" }, ...chips),
    ...lines.map((t) => el("p", { class: "notice error" }, t)),
    el("p", { class: "muted hint" }, `Auto-posting is limited to ${d.daily_limit} replies per platform per day.`));
}

function counts(items) {
  const c = { open: 0, approved: 0, posted: 0, all: items.length };
  for (const i of items) {
    if (i.status === "posted") c.posted += 1;
    else c.open += 1;
    if (i.status === "approved") c.approved += 1;
  }
  return c;
}

function render() {
  const d = st.data;
  if (!d) return;
  setupBanner(d);
  const c = counts(d.items);
  for (const tab of document.querySelectorAll("#rq-tabs [data-filter]")) {
    tab.setAttribute("aria-selected", String(tab.dataset.filter === st.filter));
    tab.querySelector(".n").textContent = c[tab.dataset.filter];
  }
  const inTab = (i) => ({ all: true, posted: i.status === "posted", approved: i.status === "approved",
    open: i.status !== "posted" })[st.filter];
  const items = d.items.filter(inTab);
  $("rq-list").replaceChildren(...(items.length ? items.map((i) => card(i, d)) : [el("p", { class: "empty" },
    d.items.length ? "Nothing in this tab." : "No replies yet. In Social listening > Open queries, open a question's answers and click \"Add to reply queue\".")]));
  if (st.highlight) {
    document.getElementById(`rq-${st.highlight}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    st.highlight = null;
  }
}

function card(item, d) {
  const posted = item.status === "posted";
  const box = el("textarea", { class: "answer-input", rows: 6, maxlength: 3000, disabled: posted || null,
    "aria-label": `Reply to: ${item.question}`, placeholder: "Write the reply, or draft it with Claude…" });
  box.value = item.reply || "";
  const charCount = el("span", { class: "muted hint" }, `${box.value.length} characters`);
  box.addEventListener("input", () => { charCount.textContent = `${box.value.length} characters · unsaved`; });
  const status = el("span", { class: "muted hint", role: "status", "data-status-for": item.id });

  const needReviewer = (fn) => () => {
    if (!reviewer()) { status.textContent = "Enter your name (Reviewer) at the top first."; $("rq-reviewer").focus(); return; }
    fn();
  };
  const btn = (text, onclick, cls = "btn-ghost") => el("button", { type: "button", class: `btn ${cls} btn-sm`, onclick }, text);
  const actions = [];
  if (!posted) {
    if (d.drafting) actions.push(btn(item.reply ? "Redraft with Claude" : "Draft with Claude",
      () => act(item, () => api(`/api/replies/${item.id}/draft`, "POST"), "Drafting with Claude…")));
    actions.push(btn("Save edit", () => act(item, () => api(`/api/replies/${item.id}`, "PUT", { reply: box.value }), "Saving…")));
    if (item.status === "draft") {
      actions.push(btn("Approve", needReviewer(() => act(item, async () => {
        if (box.value !== (item.reply || "")) await api(`/api/replies/${item.id}`, "PUT", { reply: box.value });
        return api(`/api/replies/${item.id}/approve`, "POST", { approver: reviewer() });
      }, "Checking & approving…")), "btn-primary"));
    }
    if ((item.status === "approved" || item.status === "failed") && item.approved_by) {
      if (item.auto_post) {
        actions.push(btn(`Post to ${label(item.platform)}`, needReviewer(() => {
          if (!confirm(`Publish this reply publicly on ${label(item.platform)} as the brand account?\n\n${item.reply}`)) return;
          act(item, () => api(`/api/replies/${item.id}/post`, "POST", { by: reviewer(), confirm: true }), "Posting…");
        }), "btn-primary"));
      }
      actions.push(btn("Mark as posted", needReviewer(() => {
        const url = prompt("Link to the reply you posted (optional):", "");
        if (url === null) return;
        act(item, () => api(`/api/replies/${item.id}/mark-posted`, "POST", { by: reviewer(), url: url.trim() || null }), "Saving…");
      })));
    }
  }
  const copy = btn("Copy reply", async () => {
    try { await navigator.clipboard.writeText(box.value); status.textContent = "Copied ✓"; } catch { status.textContent = "Select the text and copy it manually."; }
  });
  actions.push(copy, link(item.source_url, "Open question ↗"));
  if (!posted) {
    actions.push(btn("Delete", () => {
      if (confirm("Remove this reply from the queue?")) act(item, () => api(`/api/replies/${item.id}`, "DELETE"), "Deleting…");
    }));
  }

  const checks = [];
  if (item.reply) {
    checks.push(el("li", { class: item.disclosure_ok ? "ok" : "bad" },
      item.disclosure_ok ? `Names ${d.brand_name} (official account disclosed)` : `Must name ${d.brand_name || "the institution (set BRAND_NAME)"}`));
    checks.push(...(item.nmc_flags.length
      ? item.nmc_flags.map((f) => el("li", { class: "bad" }, `NMC: "${f.match}", ${f.issue} (${f.clause})`))
      : [el("li", { class: "ok" }, "No NMC claim patterns found")]));
    if (/\[[^\]]+\]/.test(item.reply)) checks.push(el("li", { class: "bad" }, "Contains [PLACEHOLDER]s to fill in"));
  }
  const comp = item.compliance;
  const compNotes = comp ? el("p", { class: "muted hint" }, `Claude's check: ${comp.verdict}${comp.notes?.length ? ` · ${comp.notes.join(" · ")}` : ""}`) : null;

  const meta = [
    item.approved_by ? `Approved by ${item.approved_by} · ${String(item.approved_at).slice(0, 16).replace("T", " ")}` : null,
    posted ? `Posted ${item.post_method === "api" ? "via API" : "by hand"} by ${item.posted_by} · ${String(item.posted_at).slice(0, 16).replace("T", " ")}` : null,
  ].filter(Boolean);

  return el("article", { class: `card rq-card is-${item.status}`, id: `rq-${item.id}` },
    el("div", { class: "rq-head" },
      el("span", { class: "nowrap" }, dot(item.platform), " ", label(item.platform)),
      el("span", { class: `rq-status s-${item.status}` }, STATUS_LABEL[item.status] || item.status),
      el("span", { class: "muted hint" }, item.post_route)),
    el("h3", { class: "rq-question" }, link(item.source_url, item.question)),
    item.context?.length ? el("details", { class: "answers-panel" }, el("summary", {}, `Existing answers (${item.context.length})`),
      el("ul", { class: "answers" }, ...item.context.map((t) => el("li", { class: "answer" }, el("p", { class: "answer-text" }, t))))) : null,
    box, charCount,
    checks.length ? el("ul", { class: "rq-checks" }, ...checks) : null,
    compNotes,
    item.error ? el("p", { class: "notice error" }, item.error) : null,
    meta.length ? el("p", { class: "muted hint" }, meta.join(" · ")) : null,
    item.posted_url ? el("p", {}, link(item.posted_url, "View the posted reply ↗")) : null,
    el("div", { class: "answer-actions" }, ...actions, status),
    el("details", { class: "rq-log" }, el("summary", { class: "muted hint" }, "History"),
      el("ul", {}, ...item.log.map((e) => el("li", { class: "muted hint" },
        `${String(e.at).slice(0, 16).replace("T", " ")} · ${e.event}${e.by ? ` · ${e.by}` : ""}${e.detail ? ` · ${e.detail}` : ""}`)))));
}
