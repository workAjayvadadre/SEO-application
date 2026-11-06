"""Questions people typically put to AI assistants (ChatGPT, Gemini, Claude, Perplexity...) about a topic.

AnswerSocrates samples the assistants directly. We cannot observe real assistant prompts, so this is an
estimate written by Claude from its knowledge of how people phrase conversational questions. It is
always labelled "estimated" in the UI and exports, and never mixed into measured search data.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from .config import Settings
from .regional import nmc_flags
from .scriptgen import FALLBACK_BETA, ScriptGenError

_S = {"type": "string"}
SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["questions"],
    "properties": {"questions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["question", "stage", "label", "why_people_ask"],
        "properties": {
            "question": _S,
            "stage": {"type": "string", "enum": ["exploring", "comparing", "deciding", "in treatment", "after treatment"]},
            "label": {"type": "string", "enum": ["TOFU", "MOFU", "BOFU", "Local", "Location", "Long Tail"]},
            "why_people_ask": _S,
        }}}},
}

SYSTEM = """List the questions real people most often type into AI assistants (ChatGPT, Gemini, Claude,
Perplexity, Copilot) about the given topic, in the given country and region. Write them the way people
actually type them: full conversational sentences, often personal ("I'm 38 and...", "my AMH is low..."),
including Hinglish or regional phrasing where that is common. Cover every stage from first exploring to
after treatment, plus cost, choosing a clinic and myths. Include region-specific questions when a region
is given. Do not invent brand names, statistics or prices. Return 25-35 distinct questions."""


async def ai_assistant_questions(settings: Settings, keyword: str, country: str = "in", region: str | None = None,
                                 client: anthropic.AsyncAnthropic | None = None) -> dict[str, Any]:
    if client is None:
        if not settings.anthropic_api_key:
            raise ScriptGenError("Set ANTHROPIC_API_KEY in .env (and restart the server) for AI-assistant questions.")
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    request = {"topic": keyword, "country": country.upper(), "region": region or "whole country"}
    try:
        response = await client.beta.messages.create(
            model=settings.content_model, max_tokens=8000, betas=[FALLBACK_BETA], fallbacks="default",
            system=SYSTEM, output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": json.dumps(request, ensure_ascii=False)}])
    except anthropic.AuthenticationError as exc:
        raise ScriptGenError("ANTHROPIC_API_KEY was rejected. Check the key in .env.") from exc
    except anthropic.APIStatusError as exc:
        raise ScriptGenError(f"Claude API error {exc.status_code}. Try again shortly.") from exc
    except anthropic.APIConnectionError as exc:
        raise ScriptGenError("Could not reach the Claude API (network).") from exc
    if response.stop_reason in {"refusal", "max_tokens"}:
        raise ScriptGenError("Could not generate AI-assistant questions for this topic. Try again.")
    data = json.loads(next(b.text for b in response.content if b.type == "text"))
    questions = []
    seen: set[str] = set()
    for q in data["questions"]:
        key = q["question"].strip().casefold()
        if key and key not in seen:
            seen.add(key)
            questions.append({**q, "estimated": True,
                              "nmc_note": "; ".join(f["issue"] for f in nmc_flags(q["question"])) or None})
    return {"keyword": keyword, "country": country, "region": region, "questions": questions,
            "note": "Estimated by AI from typical phrasing; not measured from real assistant traffic.",
            "model": response.model}
