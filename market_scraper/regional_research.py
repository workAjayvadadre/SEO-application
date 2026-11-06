"""Regional context for a territory and month, researched live on the web by Claude.

Claude uses the server-side web search tool, then reports through a strict `submit_regional_brief`
tool so the result is schema-valid JSON. Briefs are cached on disk per territory + month + channel
(14 days) because festivals and local context don't change hourly and each run costs API credits.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import anthropic

from .config import Settings
from .geo import Territory
from .regional import events_for, languages_for
from .scriptgen import FALLBACK_BETA, ScriptGenError

log = logging.getLogger(__name__)
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "regional"
CACHE_SECONDS = 14 * 24 * 3600

_S = {"type": "string"}
_STRS = {"type": "array", "items": _S}
BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["territory_summary", "languages", "events", "seasonal_context", "cultural_nuances",
                 "health_context", "audience_insights", "channel_notes", "local_search_terms", "sensitivities"],
    "properties": {
        "territory_summary": _S,
        "languages": {
            "type": "object", "additionalProperties": False,
            "required": ["primary", "others", "script", "tone_notes"],
            "properties": {"primary": _S, "others": _STRS, "script": _S, "tone_notes": _S},
        },
        "events": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["date", "name", "type", "impact", "where", "why_it_matters", "content_angle", "verify", "source"],
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD, or YYYY-MM-DD/YYYY-MM-DD for a range"},
                "name": _S,
                "type": {"type": "string", "enum": ["festival", "fair", "local_event", "health_day", "national_day",
                                                    "season", "exam_or_school", "wedding_season", "other"]},
                "impact": {"type": "string", "enum": ["high", "medium", "low"]},
                "where": _S, "why_it_matters": _S, "content_angle": _S,
                "verify": {"type": "boolean"}, "source": _S,
            }}},
        "seasonal_context": _STRS,
        "cultural_nuances": _STRS,
        "health_context": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["point", "source"],
            "properties": {"point": _S, "source": _S}}},
        "audience_insights": _STRS,
        "channel_notes": _STRS,
        "local_search_terms": _STRS,
        "sensitivities": _STRS,
    },
}

SUBMIT_TOOL = {
    "name": "submit_regional_brief",
    "description": "Submit the finished regional research brief. Call exactly once, after researching.",
    "strict": True,
    "input_schema": BRIEF_SCHEMA,
}

SYSTEM = """You are a regional market researcher for the marketing team of an IVF / fertility institution in
India. You research a territory for one month so the team can plan regionally relevant, NMC-compliant
communication (NMC advertising guidelines, Public Notice R-13014/01/2024-Ethics, 06-10-2026).

Research with web search, then call submit_regional_brief once. Rules:
- Events: festivals, fairs, local events and observances that fall in the given month AND have real
  local impact in this territory (not a generic all-India list). Rank by local impact. Give exact dates
  for the given year when you can find them from a reliable source (state holiday list, panchang,
  government or established news site); set verify=true when the date is lunar, disputed or not found.
  Add relevant health days (e.g. awareness days) and seasonal moments (weather, harvest, exams,
  wedding season) when they shape what people do and search for.
- content_angle: an NMC-compliant idea only: greetings (no offer, CTA or doctor promotion) or public
  health awareness. Never success rates, superlatives, offers, testimonials or fear-based messaging.
- Languages: the main language and dialects people actually use online in this territory, the script,
  and tone notes (formal / colloquial, code-mixing such as Hinglish).
- Health context: only sourced, general facts relevant to fertility and reproductive health in the
  region (awareness levels, myths, access, family decision-making). No invented statistics: if no
  reliable figure exists, say so qualitatively.
- Channel notes: how people in this territory use the requested channel(s) (formats, timing, language).
- Sensitivities: religious, caste, gender and cultural sensitivities to respect. No stereotyping.
- Every factual claim should be traceable: put the URL you relied on in `source`.
Keep each list item to one or two sentences."""


def _cache_path(territory: Territory, month: str, channels: tuple[str, ...]) -> Path:
    key = json.dumps([territory.to_dict()["label"], territory.zone, month, sorted(channels)], sort_keys=True)
    return CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()[:20]}.json"


def _user_location(t: Territory) -> dict[str, Any]:
    loc: dict[str, Any] = {"type": "approximate", "country": "IN", "timezone": "Asia/Kolkata"}
    if t.state:
        loc["region"] = t.state
    if t.district or t.subdistrict:
        loc["city"] = t.subdistrict or t.district
    return loc


async def regional_brief(settings: Settings, territory: Territory, month: str, channels: tuple[str, ...],
                         topic: str = "IVF and fertility care", *, refresh: bool = False,
                         client: anthropic.AsyncAnthropic | None = None) -> dict[str, Any]:
    path = _cache_path(territory, month, channels)
    if not refresh and path.is_file() and time.time() - path.stat().st_mtime < CACHE_SECONDS:
        cached = json.loads(path.read_text(encoding="utf-8"))
        return {**cached, "cached": True}

    if client is None:
        if not settings.anthropic_api_key:
            raise ScriptGenError("Set ANTHROPIC_API_KEY in .env (and restart the server) for regional research.")
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    known = []
    for state in territory.states[:10]:
        known += [f"{e['date']} {e['name']} ({state})" for e in events_for(month, state)]
    request = {
        "territory": territory.to_dict()["label"], "level": territory.level, "zone": territory.zone,
        "states_covered": list(territory.states), "month": month, "channels": list(channels), "topic": topic,
        "starting_points_to_verify": sorted(set(known))[:40],
        "likely_languages": sorted({lang for s in territory.states for lang in languages_for(s)}),
    }
    messages: list[dict[str, Any]] = [{"role": "user", "content":
        "Research this territory and month, then call submit_regional_brief.\n\n"
        + json.dumps(request, ensure_ascii=False, indent=2)}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 10,
              "user_location": _user_location(territory)}, SUBMIT_TOOL]

    sources: dict[str, str] = {}
    usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0}
    for _ in range(8):
        try:
            response = await client.beta.messages.create(
                model=settings.content_model, max_tokens=16000, betas=[FALLBACK_BETA], fallbacks="default",
                system=SYSTEM, tools=tools, tool_choice={"type": "auto"},
                output_config={"effort": settings.content_effort}, messages=messages)
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

        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens
        for block in response.content:
            if block.type == "web_search_tool_result" and isinstance(block.content, list):
                usage["web_searches"] += 1
                for hit in block.content:
                    if getattr(hit, "url", None):
                        sources.setdefault(hit.url, getattr(hit, "title", "") or hit.url)
        if response.stop_reason == "refusal":
            raise ScriptGenError("The model declined this research request.")
        submitted = next((b for b in response.content if b.type == "tool_use" and b.name == SUBMIT_TOOL["name"]), None)
        if submitted is not None:
            brief = submitted.input if isinstance(submitted.input, dict) else json.loads(submitted.input)
            result = {"territory": territory.to_dict(), "month": month, "channels": list(channels),
                      "brief": brief, "sources": [{"url": u, "title": t} for u, t in list(sources.items())[:40]],
                      "model": response.model, "usage": usage, "generated_at": int(time.time())}
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            return {**result, "cached": False}
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "pause_turn":  # finished talking without submitting: ask once more
            messages.append({"role": "user", "content": "Now call submit_regional_brief with your findings."})
    raise ScriptGenError("Regional research did not finish. Try again.")
