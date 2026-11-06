"""Claude-written answers for open queries, shown in the Answers column instead of "–".

One batched call answers every question on the page; results are cached by answer_key so a question
that comes back in a later search is not paid for twice. Answers read like a knowledgeable person
explaining things plainly, never like a patient: first-person experience would be a fake testimonial
(NMC Cl. 6.2, 8.1 v). Each answer is re-checked with regional.nmc_flags.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import anthropic

from .config import Settings
from .manual_answers import answer_key
from .regional import nmc_flags
from .scriptgen import FALLBACK_BETA, SYSTEM_PROMPT, ScriptGenError
from .utils import utc_now_iso

MAX_BATCH = 30
MAX_CACHE = 5000

ANSWER_SYSTEM = SYSTEM_PROMPT + """

TASK NOW: answer each question below the way a knowledgeable, warm person would answer it on a
forum: plain words, specific, directly answering what was asked. The same NMC rules apply in full.
- Sound human: short paragraphs, natural phrasing, no headings, no bullet lists unless steps truly
  need them, no "Great question!", no marketing tone, no hashtags, emojis or links.
- NEVER write as a patient or claim personal experience ("I did IVF", "my wife", "in my case").
  Explain, don't testify. No success rates, percentages, guarantees, "painless" or superlatives.
- Where personal factors matter, say so and suggest talking to a qualified fertility specialist.
- Do not name or promote any clinic or doctor, and do not sign the answer (the reply queue adds the
  institution's disclosure line when an answer is posted).
- Length: 60-140 words. Use the language and script of the question.
- If a question is not really about fertility/health, give a one-line neutral answer.
Return one answer per question id.
"""

SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["answers"],
    "properties": {"answers": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "answer", "language"],
        "properties": {"id": {"type": "string"}, "answer": {"type": "string"}, "language": {"type": "string"}}}}},
}


async def write_answers(settings: Settings, questions: list[dict[str, Any]], *,
                        client: anthropic.AsyncAnthropic | None = None) -> dict[str, dict[str, Any]]:
    """questions: [{key, question, platform, context}] -> {key: {answer, language, nmc_flags, ...}}."""
    if not questions:
        return {}
    if client is None:
        if not settings.anthropic_api_key:
            raise ScriptGenError("Set ANTHROPIC_API_KEY in .env (and restart the server) to write answers.")
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    ids = {f"q{i + 1}": q["key"] for i, q in enumerate(questions)}
    payload = [{"id": qid, "platform": q["platform"], "question": q["question"],
                "what_others_answered": [c[:600] for c in q.get("context", [])[:3]]}
               for qid, q in zip(ids, questions)]
    try:
        response = await client.beta.messages.create(
            model=settings.content_model, max_tokens=16000, betas=[FALLBACK_BETA], fallbacks="default",
            cache_control={"type": "ephemeral"}, system=ANSWER_SYSTEM,
            output_config={"effort": settings.content_effort, "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": "Answer these questions and return the JSON object.\n\n"
                                                  + json.dumps(payload, ensure_ascii=False, indent=2)}],
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
        raise ScriptGenError("The model declined to answer these questions.")
    if response.stop_reason == "max_tokens":
        raise ScriptGenError("The answers were cut off. Try again.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ScriptGenError("The model returned malformed content. Try again.") from exc

    now = utc_now_iso()
    out: dict[str, dict[str, Any]] = {}
    for a in data.get("answers", []):
        key = ids.get(a.get("id"))
        answer = (a.get("answer") or "").strip()
        if key and answer:
            out[key] = {"answer": answer[:3000], "language": a.get("language"), "nmc_flags": nmc_flags(answer),
                        "model": response.model, "created_at": now}
    return out


class AiAnswers:
    """Cache of written answers, keyed by answer_key. One small JSON file; memory only without a path."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        if path:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self._data = loaded if isinstance(loaded, dict) else {}
            except (FileNotFoundError, ValueError):
                self._data = {}

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._data.get(key)

    def put_many(self, answers: dict[str, dict[str, Any]], questions: dict[str, str]) -> None:
        with self._lock:
            for key, value in answers.items():
                self._data[key] = {**value, "question": questions.get(key, "")[:300]}
            if len(self._data) > MAX_CACHE:
                for k in sorted(self._data, key=lambda k: self._data[k].get("created_at", ""))[: len(self._data) - MAX_CACHE]:
                    self._data.pop(k, None)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self.path)


async def answers_for(settings: Settings, cache: AiAnswers, items: list[dict[str, Any]], *, generate: bool,
                      refresh: bool = False, client: anthropic.AsyncAnthropic | None = None
                      ) -> dict[str, dict[str, Any] | None]:
    """Cached answers for items; with generate=True, writes the missing ones (or all, with refresh)."""
    keyed = {answer_key(i["source_url"], i["question"]): i for i in items[:MAX_BATCH]}
    result = {k: (None if refresh else cache.get(k)) for k in keyed}
    missing = [{"key": k, **keyed[k]} for k, v in result.items() if v is None]
    if generate and missing:
        written = await write_answers(settings, missing, client=client)
        cache.put_many(written, {k: keyed[k]["question"] for k in written})
        result.update(written)
    return result
