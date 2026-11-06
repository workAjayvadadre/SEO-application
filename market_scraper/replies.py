"""Reply queue for open queries (ORM): draft -> human approval -> post (API) or copy & post by hand.

Rules the queue enforces, whatever the UI does:
- Nothing is posted without a named approver, and editing the text after approval withdraws it.
- Approval needs BRAND_NAME set and named in the reply (NMC Cl. 8.3 i: the institution is identifiable),
  and no NMC claim flags in the text (regional.nmc_flags), re-checked again at post time.
- One reply per question/thread, and at most REPLY_DAILY_LIMIT auto-posts per platform per day.
- Every step is logged with who and when, for the compliance record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any

import anthropic

from .config import Settings
from .manual_answers import answer_key
from .publishers import MAX_LENGTH, PublishError, capabilities, publish, target_for
from .regional import nmc_flags
from .scriptgen import FALLBACK_BETA, SYSTEM_PROMPT, ScriptGenError
from .utils import utc_now_iso

log = logging.getLogger(__name__)

MAX_REPLY_CHARS = 3000
MAX_ITEMS = 2000
STATUSES = ("draft", "approved", "posted", "failed")


class ReplyError(RuntimeError):
    """User-facing refusal (HTTP 400/409)."""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def disclosure_ok(text: str, brand: str | None) -> bool:
    return bool(brand) and brand.casefold() in (text or "").casefold()


# ------------------------------------------------------------------ drafting (Claude)

REPLY_SYSTEM = SYSTEM_PROMPT + """

TASK NOW: write ONE public reply that the institution's official account will post under someone's
question on a social platform (online reputation management). The same NMC rules apply in full.
- Class A (education) by default; class B only for plain facts the institution must supply as [PLACEHOLDER].
- Answer the actual question helpfully, kindly and specifically, in plain language. Correct
  misinformation in the existing answers politely, without naming or criticising anyone.
- Open with the substance, not a greeting formula. No hashtags, no emojis, no links, no phone numbers,
  no booking invitation. The only call to action allowed: suggest consulting a qualified fertility
  specialist for personal advice.
- Disclose who is speaking: end with a line like "- [Institution name] team (official account)" using the
  institution_name given. Never pretend to be a patient or an independent person.
- Never ask for or refer to the asker's medical details; no diagnosis for an individual.
- Fit the platform: Reddit and Quora up to ~150 words; YouTube, Instagram and Facebook up to ~80 words.
- Write in the language of the question (Hindi or Hinglish replies in the same script the asker used).
"""

REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reply", "language", "content_class", "compliance"],
    "properties": {
        "reply": {"type": "string"},
        "language": {"type": "string"},
        "content_class": {"type": "string", "enum": ["A", "B"]},
        "compliance": {
            "type": "object", "additionalProperties": False,
            "required": ["verdict", "notes", "placeholders"],
            "properties": {"verdict": {"type": "string", "enum": ["GREEN", "AMBER"]},
                           "notes": {"type": "array", "items": {"type": "string"}},
                           "placeholders": {"type": "array", "items": {"type": "string"}}},
        },
    },
}


async def draft_reply(settings: Settings, item: dict[str, Any], *,
                      client: anthropic.AsyncAnthropic | None = None) -> dict[str, Any]:
    if client is None:
        if not settings.anthropic_api_key:
            raise ScriptGenError("Set ANTHROPIC_API_KEY in .env (and restart the server) to draft replies.")
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    brief = {
        "platform": item["platform"], "question": item["question"], "question_link": item["source_url"],
        "existing_answers": item.get("context", [])[:5],
        "institution_name": settings.brand_name or "[INSTITUTION NAME]",
    }
    try:
        response = await client.beta.messages.create(
            model=settings.content_model, max_tokens=4000, betas=[FALLBACK_BETA], fallbacks="default",
            cache_control={"type": "ephemeral"}, system=REPLY_SYSTEM,
            output_config={"effort": settings.content_effort, "format": {"type": "json_schema", "schema": REPLY_SCHEMA}},
            messages=[{"role": "user", "content": "Write the reply for this question and return the JSON object.\n\n"
                                                  + json.dumps(brief, ensure_ascii=False, indent=2)}],
        )
    except anthropic.AuthenticationError as exc:
        raise ScriptGenError("ANTHROPIC_API_KEY was rejected. Check the key in .env.") from exc
    except anthropic.RateLimitError as exc:
        raise ScriptGenError("Claude API rate limit reached. Try again in a minute.") from exc
    except anthropic.BadRequestError as exc:
        raise ScriptGenError(f"Claude API rejected the request: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise ScriptGenError(f"Claude API error {exc.status_code}. Try again shortly.") from exc
    except anthropic.APIConnectionError as exc:
        raise ScriptGenError("Could not reach the Claude API (network).") from exc
    if response.stop_reason == "refusal":
        raise ScriptGenError("The model declined to answer this question. Write the reply by hand.")
    if response.stop_reason == "max_tokens":
        raise ScriptGenError("The reply was cut off. Try again.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ScriptGenError("The model returned malformed content. Try again.") from exc


# ------------------------------------------------------------------ queue

class ReplyQueue:
    def __init__(self, settings: Settings, path: Path | None = None) -> None:
        self.settings = settings
        self.path = path
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}
        if path:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self._items = {i["id"]: i for i in loaded if isinstance(i, dict) and "id" in i}
            except (FileNotFoundError, ValueError):
                self._items = {}
        for item in self._items.values():  # server stopped mid-post: the outcome is unknown
            if item.get("status") == "posting":
                item.update(status="failed", error="Posting was interrupted. Check the thread before retrying: "
                                                   "the reply may already be live.")

    # -------------------------------------------------- reads

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self._items.values(), key=lambda i: i["created_at"], reverse=True)
            return [self._view(i) for i in items]

    def get(self, item_id: str) -> dict[str, Any]:
        with self._lock:
            return self._view(self._require(item_id))

    def _view(self, item: dict[str, Any]) -> dict[str, Any]:
        target = target_for(item["platform"], item["source_url"], item.get("graph_id"))
        caps = capabilities(self.settings).get(item["platform"], {"auto": False, "reason": "No posting API"})
        if not target and not caps["auto"]:
            route = caps["reason"]  # e.g. Quora: no posting API at all
        elif not target:
            route = "No API target for this item: copy the reply and post it by hand"
        elif caps["auto"]:
            route = target.describe()
        else:
            route = caps["reason"]
        return {**item,
                "nmc_flags": nmc_flags(item.get("reply", "")),
                "disclosure_ok": disclosure_ok(item.get("reply", ""), self.settings.brand_name),
                "auto_post": bool(target and caps["auto"]),
                "post_route": route}

    def _require(self, item_id: str) -> dict[str, Any]:
        item = self._items.get(item_id)
        if not item:
            raise KeyError(item_id)
        return item

    # -------------------------------------------------- writes

    def add(self, *, question: str, platform: str, record_type: str, source_url: str,
            graph_id: str | None = None, context: list[str] | None = None,
            reply: str | None = None) -> dict[str, Any]:
        key = answer_key(source_url, question)
        start = self._with_disclosure(reply) if reply and reply.strip() else ""
        with self._lock:
            existing = next((i for i in self._items.values() if i["key"] == key), None)
            if existing:
                if start and not existing.get("reply") and existing["status"] == "draft":
                    existing["reply"] = start
                    self._log(existing, "started from the written answer")
                return self._view(existing)
            now = utc_now_iso()
            item = {"id": uuid.uuid4().hex, "key": key, "created_at": now, "updated_at": now,
                    "question": question[:300], "platform": platform, "record_type": record_type,
                    "source_url": source_url, "graph_id": graph_id,
                    "context": [c[:2000] for c in (context or []) if c][:5],
                    "reply": start, "status": "draft", "compliance": None, "approved_by": None, "approved_at": None,
                    "approved_hash": None, "posted_at": None, "posted_url": None, "posted_by": None,
                    "post_method": None, "error": None, "log": [{"at": now, "event": "added"}]}
            self._items[item["id"]] = item
            if len(self._items) > MAX_ITEMS:  # drop the oldest unposted drafts
                for old in sorted((i for i in self._items.values() if i["status"] == "draft"),
                                  key=lambda i: i["created_at"])[: len(self._items) - MAX_ITEMS]:
                    self._items.pop(old["id"], None)
            self._write()
            return self._view(item)

    def set_draft(self, item_id: str, drafted: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            item = self._require(item_id)
            self._editable(item)
            item.update(reply=(drafted.get("reply") or "")[:MAX_REPLY_CHARS], compliance=drafted.get("compliance"),
                        status="draft", approved_by=None, approved_at=None, approved_hash=None, error=None)
            self._log(item, "drafted by Claude")
            return self._view(item)

    def edit(self, item_id: str, reply: str) -> dict[str, Any]:
        with self._lock:
            item = self._require(item_id)
            self._editable(item)
            was_approved = item["status"] == "approved"
            item.update(reply=reply[:MAX_REPLY_CHARS], status="draft", approved_by=None, approved_at=None,
                        approved_hash=None, error=None)
            self._log(item, "edited" + (" (approval withdrawn)" if was_approved else ""))
            return self._view(item)

    def approve(self, item_id: str, approver: str) -> dict[str, Any]:
        with self._lock:
            item = self._require(item_id)
            self._editable(item)
            self._check_postable_text(item)
            item.update(status="approved", approved_by=approver, approved_at=utc_now_iso(),
                        approved_hash=_hash(item["reply"]), error=None)
            self._log(item, "approved", by=approver)
            return self._view(item)

    def delete(self, item_id: str) -> None:
        with self._lock:
            item = self._require(item_id)
            if item["status"] == "posted":
                raise ReplyError("A posted reply stays in the log. Remove it on the platform itself if needed.")
            self._items.pop(item_id)
            self._write()

    def mark_posted(self, item_id: str, url: str | None, by: str) -> dict[str, Any]:
        """Record a reply your team posted by hand (Quora, or anywhere without an API)."""
        with self._lock:
            item = self._require(item_id)
            self._ready_to_post(item)
            item.update(status="posted", posted_at=utc_now_iso(), posted_url=url, posted_by=by,
                        post_method="manual", error=None)
            self._log(item, "marked as posted by hand", by=by, detail=url)
            return self._view(item)

    async def post(self, item_id: str, by: str, *, transport=None) -> dict[str, Any]:
        with self._lock:
            item = self._require(item_id)
            self._ready_to_post(item)
            target = target_for(item["platform"], item["source_url"], item.get("graph_id"))
            if not target:
                raise ReplyError("This question can't be answered through an API. Copy the reply, post it by hand, "
                                 "then use 'Mark as posted'.")
            today = utc_now_iso()[:10]
            sent_today = sum(1 for i in self._items.values() if i["platform"] == item["platform"]
                             and i.get("post_method") == "api" and (i.get("posted_at") or "").startswith(today))
            if sent_today >= self.settings.reply_daily_limit:
                raise ReplyError(f"Daily limit reached: {sent_today} {item['platform']} replies auto-posted today "
                                 f"(REPLY_DAILY_LIMIT={self.settings.reply_daily_limit}). This protects the account "
                                 "from being flagged as spam.")
            item["status"] = "posting"  # blocks a second click while the request is out
            text = item["reply"]
        try:
            url = await publish(self.settings, target, text, transport=transport)
        except PublishError as exc:
            with self._lock:
                item.update(status="failed", error=str(exc))
                self._log(item, "post failed", by=by, detail=str(exc))
                return self._view(item)
        with self._lock:
            item.update(status="posted", posted_at=utc_now_iso(), posted_url=url, posted_by=by,
                        post_method="api", error=None)
            self._log(item, f"posted via {target.platform} API", by=by, detail=url)
            return self._view(item)

    def _with_disclosure(self, text: str) -> str:
        text = text.strip()[:MAX_REPLY_CHARS - 80]
        brand = self.settings.brand_name
        if brand and disclosure_ok(text, brand):
            return text
        return f"{text}\n\n- {brand or '[INSTITUTION NAME]'} team (official account)"

    # -------------------------------------------------- rules

    @staticmethod
    def _editable(item: dict[str, Any]) -> None:
        if item["status"] in ("posted", "posting"):
            raise ReplyError("This reply has already been posted (or is being posted) and can't be changed.")

    def _check_postable_text(self, item: dict[str, Any]) -> None:
        text = item.get("reply", "").strip()
        if not text:
            raise ReplyError("Write or draft the reply first.")
        if not self.settings.brand_name:
            raise ReplyError("Set BRAND_NAME in .env (and restart): official replies must name the institution "
                             "(NMC Cl. 8.3 i).")
        if not disclosure_ok(text, self.settings.brand_name):
            raise ReplyError(f"The reply must name \"{self.settings.brand_name}\" so readers know it's the "
                             "institution's official account.")
        if "[" in text and "]" in text:
            raise ReplyError("Fill in or remove every [PLACEHOLDER] before approving.")
        flags = nmc_flags(text)
        if flags:
            raise ReplyError("NMC check failed: " + "; ".join(f"\"{f['match']}\" ({f['issue']}, {f['clause']})"
                                                               for f in flags))
        limit = MAX_LENGTH.get(item["platform"])
        if limit and len(text) > limit:
            raise ReplyError(f"Too long for {item['platform'].title()} ({len(text)} of {limit} characters).")

    def _ready_to_post(self, item: dict[str, Any]) -> None:
        if item["status"] == "posted":
            raise ReplyError("Already posted.")
        if item["status"] == "posting":
            raise ReplyError("This reply is being posted right now.")
        if item["status"] not in ("approved", "failed") or not item.get("approved_by"):
            raise ReplyError("Approve the reply first.")
        if item.get("approved_hash") != _hash(item.get("reply", "")):
            raise ReplyError("The text changed after approval. Approve it again.")
        self._check_postable_text(item)  # rules may have changed (e.g. BRAND_NAME) since approval

    def _log(self, item: dict[str, Any], event: str, *, by: str | None = None, detail: str | None = None) -> None:
        now = utc_now_iso()
        item["updated_at"] = now
        item["log"].append({k: v for k, v in {"at": now, "event": event, "by": by, "detail": detail}.items() if v})
        self._write()

    def _write(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self._items.values()), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
