// Shared helpers. All external text goes through el(), which only ever uses textContent.

export const LABELS = {
  google: "Google", reddit: "Reddit", quora: "Quora",
  youtube: "YouTube", instagram: "Instagram", facebook: "Facebook",
};

export const $ = (id) => document.getElementById(id);

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** Only http(s) links are rendered as links. Anything else (javascript:, data:) is dropped. */
export function safeUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

export function link(href, text) {
  const url = safeUrl(href);
  return url ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, text) : el("span", {}, text);
}

export const dot = (platform) => el("span", { class: `dot p-${platform}`, "aria-hidden": "true" });
export const label = (platform) => LABELS[platform] || platform;

export function download(name, type, content) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const a = el("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function csvCell(value) {
  let text = value == null ? "" : String(value);
  if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`; // stop spreadsheets executing scraped text as formulas
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function toCsv(rows, cols) {
  const lines = [cols.join(","), ...rows.map((r) => cols.map((c) => csvCell(r[c])).join(","))];
  return "﻿" + lines.join("\r\n"); // BOM so Excel reads UTF-8
}

export function slug(text) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "results";
}

export function describeError(detail) {
  if (!detail) return null;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg?.replace(/^Value error, /, "")).filter(Boolean).join("; ");
  return null;
}
