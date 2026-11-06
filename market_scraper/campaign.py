"""Regional monthly campaign for one channel: calendar slots, each with a deliverable brief.

Inputs are the regional research brief (regional_research.py), keyword research for the territory and,
optionally, audience questions from social listening. Claude turns them into a channel-specific calendar.
The team writes the final content; each slot says what that content must achieve and contain.
Google Ads gets weekly ad groups (keywords, negatives, geo, RSA assets) instead of posts.
"""

from __future__ import annotations

import calendar as cal
import json
import logging
from typing import Any

import anthropic

from .config import Settings
from .geo import Territory
from .regional import nmc_flags
from .scriptgen import FALLBACK_BETA, SYSTEM_PROMPT as NMC_RULES, ScriptGenError, post_check

log = logging.getLogger(__name__)

CAMPAIGN_CHANNELS: dict[str, dict[str, Any]] = {
    "instagram": {"label": "Instagram", "formats": ["Reel", "Carousel", "Static post", "Story series"], "per_week": 4},
    "youtube": {"label": "YouTube", "formats": ["Long video", "Short"], "per_week": 2},
    "facebook": {"label": "Facebook", "formats": ["Post", "Q&A post", "Video post"], "per_week": 3},
    "blog": {"label": "Website blog", "formats": ["Blog post", "FAQ page", "Guide"], "per_week": 2},
    "gbp": {"label": "Google Business Profile", "formats": ["Update post", "Q&A"], "per_week": 2},
    "google_ads": {"label": "Google Ads (Search)", "formats": ["Ad group"], "per_week": 1},
}

_S = {"type": "string"}
_STRS = {"type": "array", "items": _S}
_INT = {"type": "integer"}

CAMPAIGN_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["strategy", "items", "ad_groups"],
    "properties": {
        "strategy": {
            "type": "object", "additionalProperties": False,
            "required": ["objective", "audience", "regional_hook", "language_plan", "tone", "key_themes",
                         "posting_guidance", "measurement"],
            "properties": {"objective": _S, "audience": _S, "regional_hook": _S, "language_plan": _S, "tone": _S,
                           "key_themes": _STRS, "posting_guidance": _S, "measurement": _STRS},
        },
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["date", "week", "format", "title", "theme", "target_keyword", "regional_hook", "why_now",
                         "nmc_class", "language", "deliverable"],
            "properties": {
                "date": _S, "week": _INT, "format": _S, "title": _S, "theme": _S, "target_keyword": _S,
                "regional_hook": _S, "why_now": _S,
                "nmc_class": {"type": "string", "enum": ["A", "B", "C"]},
                "language": _S,
                "deliverable": {
                    "type": "object", "additionalProperties": False,
                    "required": ["objective", "key_messages", "questions_to_answer", "outline", "must_include",
                                 "avoid", "visual_direction", "allowed_cta", "evidence", "kpis"],
                    "properties": {"objective": _S, "key_messages": _STRS, "questions_to_answer": _STRS,
                                   "outline": _STRS, "must_include": _STRS, "avoid": _STRS,
                                   "visual_direction": _S, "allowed_cta": _S, "evidence": _STRS, "kpis": _STRS},
                },
            }}},
        "ad_groups": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["week", "name", "theme", "keywords", "negative_keywords", "geo_targets", "schedule",
                         "headlines", "descriptions", "landing_page_brief", "nmc_notes"],
            "properties": {"week": _INT, "name": _S, "theme": _S, "keywords": _STRS, "negative_keywords": _STRS,
                           "geo_targets": _STRS, "schedule": _S, "headlines": _STRS, "descriptions": _STRS,
                           "landing_page_brief": _S, "nmc_notes": _S},
        }},
    },
}

PLANNER_SYSTEM = NMC_RULES + """

You are now planning, not writing final content. The marketing team writes the content; you give them a
regional calendar and, for every slot, a deliverable brief: what the piece must achieve, the key messages,
the audience questions it must answer, an outline, what it must include and avoid, visual direction,
the only CTA allowed under NMC, the evidence from the research behind it, and KPIs.

Make the plan genuinely regional: build on the territory's events (respect their impact and dates;
mark lunar dates "(verify date)" in why_now), language and code-mixing, season, cultural nuances, local
search terms and audience questions. Greeting posts on festivals are Class C (wishes only). Awareness
content is Class A. Factual institutional information (address, services, timings) is Class B; it is the
only class that may carry the institution's contact as a CTA. Spread themes across the month; do not
repeat a theme in consecutive slots. Dates must fall inside the month.

Google Ads (only when the channel is google_ads): put one ad group per week in `ad_groups` and leave
`items` with one slot per ad group describing its landing page as the deliverable. Ads are Class B
factual: no superlatives, success rates, offers, urgency or doctor names. Headlines max 30 characters
(give 10-15), descriptions max 90 characters (give 4). Include negative keywords that keep spend off
job seekers, free treatment, donor / surrogate solicitation and competitor brand names. Geo targets use
the territory and nearby areas; note language targeting. For every other channel return ad_groups as [].
"""


def month_dates(month: str) -> tuple[str, str]:
    year, mon = (int(x) for x in month.split("-"))
    return f"{month}-01", f"{month}-{cal.monthrange(year, mon)[1]:02d}"


def _compact_keywords(keywords: list[dict[str, Any]], limit: int = 120) -> list[str]:
    keywords = [k for k in keywords if k.get("brand") != "competitor"]  # never plan around rival brands
    local = [k for k in keywords if k.get("label") in {"Local", "Location"} or k.get("group") == "local"]
    rest = [k for k in keywords if k not in local]
    rest.sort(key=lambda k: (k.get("position", 99), len(k.get("keyword", ""))))
    picked = (local + rest)[:limit]
    return [f"{k['keyword']} [{k.get('label') or k.get('intent', '')}]" for k in picked]


def check_ads(ad_groups: list[dict[str, Any]]) -> list[dict[str, str]]:
    problems = []
    for g in ad_groups:
        for h in g.get("headlines", []):
            if len(h) > 30:
                problems.append({"where": f"{g['name']} headline", "issue": f"{len(h)} characters (max 30)", "text": h})
        for d in g.get("descriptions", []):
            if len(d) > 90:
                problems.append({"where": f"{g['name']} description", "issue": f"{len(d)} characters (max 90)", "text": d})
        for text in g.get("headlines", []) + g.get("descriptions", []):
            for f in nmc_flags(text):
                problems.append({"where": f"{g['name']} ad copy", "issue": f"{f['issue']} (Cl. {f['clause']})", "text": text})
    return problems


async def build_campaign(settings: Settings, territory: Territory, month: str, channel: str, per_week: int,
                         regional: dict[str, Any], keywords: list[dict[str, Any]],
                         questions: list[str] | None = None, topic: str = "IVF and fertility care",
                         client: anthropic.AsyncAnthropic | None = None) -> dict[str, Any]:
    if channel not in CAMPAIGN_CHANNELS:
        raise ValueError(f"unknown channel {channel}")
    if client is None:
        if not settings.anthropic_api_key:
            raise ScriptGenError("Set ANTHROPIC_API_KEY in .env (and restart the server) to build campaigns.")
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    info = CAMPAIGN_CHANNELS[channel]
    start, end = month_dates(month)
    weeks = 5 if int(end[-2:]) > 28 else 4
    request = {
        "institution_name": settings.brand_name or "[INSTITUTION NAME]",
        "topic": topic, "territory": territory.to_dict(), "month": month, "date_range": [start, end],
        "channel": channel, "channel_label": info["label"], "formats": info["formats"],
        "slots": per_week * weeks if channel != "google_ads" else weeks,
        "regional_research": regional.get("brief", {}),
        "keyword_research": _compact_keywords(keywords),
        "audience_questions_from_social": (questions or [])[:40],
    }
    try:
        async with client.beta.messages.stream(
            model=settings.content_model, max_tokens=64000, betas=[FALLBACK_BETA], fallbacks="default",
            system=PLANNER_SYSTEM,
            output_config={"effort": settings.content_effort,
                           "format": {"type": "json_schema", "schema": CAMPAIGN_SCHEMA}},
            messages=[{"role": "user", "content": "Plan this regional campaign and return the JSON object.\n\n"
                       + json.dumps(request, ensure_ascii=False, indent=2)}],
        ) as stream:
            response = await stream.get_final_message()
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
        raise ScriptGenError("The model declined this request.")
    if response.stop_reason == "max_tokens":
        raise ScriptGenError("The plan was cut off. Try fewer posts per week.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        plan = json.loads(text)
    except ValueError as exc:
        raise ScriptGenError("The model returned a malformed plan. Try again.") from exc

    plan["items"] = sorted((i for i in plan["items"] if start <= i["date"] <= end), key=lambda i: i["date"])
    return {
        "territory": territory.to_dict(), "month": month, "channel": channel, "channel_label": info["label"],
        "plan": plan,
        "auto_check": post_check({"items": plan["items"], "strategy": plan["strategy"]}),
        "ads_check": check_ads(plan.get("ad_groups", [])),
        "model": response.model,
        "usage": {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
    }
