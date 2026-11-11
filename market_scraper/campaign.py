"""Regional monthly campaign for one channel: calendar slots, each with a deliverable brief.

Inputs are the regional research brief (regional_research.py) and three data sources: keyword research
(Keyword Studio results or a fresh search), Google Trends for the top keywords, and social listening insights
(open questions, pain points, trending topics, sentiment). Claude turns them into a channel-specific calendar
and says, per slot, which data each choice was built from; check_sources() verifies that afterwards.
The team writes the final content; each slot says what that content must achieve and contain.
Google Ads gets weekly ad groups (keywords, negatives, geo, RSA assets) instead of posts.
"""

from __future__ import annotations

import calendar as cal
import json
import logging
import re
from typing import Any

import anthropic
import httpx

from .config import Settings
from .geo import Territory
from .regional import nmc_flags
from .scriptgen import FALLBACK_BETA, SYSTEM_PROMPT as NMC_RULES, ScriptGenError, post_check
from .trends import TrendsError, google_trends

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
_BUILT_FROM = {"type": "object", "additionalProperties": False,
               "required": ["keyword_source", "trend_signal", "social_insight"],
               "properties": {"keyword_source": _S, "trend_signal": _S, "social_insight": _S}}

TRENDS_TIMEFRAME = "quarter"  # trends.TIMEFRAMES: last 3 months, so change_pct is first month vs last month
TRENDS_KEYWORDS = 5           # Google Trends compares at most 5 terms in one request
LIMITS = {"keywords": 120, "questions": 30, "pain_points": 10, "trending_topics": 10, "rising_related": 5}

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
                         "nmc_class", "language", "built_from", "deliverable"],
            "properties": {
                "date": _S, "week": _INT, "format": _S, "title": _S, "theme": _S, "target_keyword": _S,
                "regional_hook": _S, "why_now": _S, "built_from": _BUILT_FROM,
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
                         "headlines", "descriptions", "landing_page_brief", "nmc_notes", "built_from"],
            "properties": {"week": _INT, "name": _S, "theme": _S, "keywords": _STRS, "negative_keywords": _STRS,
                           "geo_targets": _STRS, "schedule": _S, "headlines": _STRS, "descriptions": _STRS,
                           "landing_page_brief": _S, "nmc_notes": _S, "built_from": _BUILT_FROM},
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

Three data sources drive the plan. Use every one that is present in the request:
1. keyword_research decides each slot's target_keyword. Copy it exactly from the list, without the
   bracketed label. Prefer local terms. Never target a competitor brand.
2. google_trends (relative interest in the territory's state, or India, over the last 3 months) decides timing
   and weight. Rising topics go earlier in the month and get more slots; falling topics get fewer slots;
   steady topics fill the rest. Rising related searches are angles worth covering when they fit the topic
   and the NMC rules.
3. social_listening (open questions, pain points, trending topics and sentiment from real people) decides
   each slot's angle and its questions_to_answer. Answer pain points with empathy and facts, never promises.

For every item and ad group fill built_from with plain strings:
- keyword_source: the research keyword the slot targets, exactly as in keyword_research; "" if none.
- trend_signal: the Trends fact that set the timing or weight, e.g. "ivf cost rising +42%"; "" if none.
- social_insight: the question, pain point or topic behind the angle, quoted briefly; "" if none.
Leave a field "" when its source is missing or unavailable. Never invent data that is not in the request.

Class C greeting and festival slots are exempt: wishes only under NMC. Set their target_keyword and every
built_from field to "", and do not put keywords, pain points or audience questions into them.

Google Ads with these sources: every ad group keyword comes from keyword_research (exact text; match-type
symbols allowed); ad groups for rising terms take the earlier weeks; pain points shape headlines and
descriptions (still Class B factual, no promises); off-target topics people raise in social listening
(jobs, free treatment, donor or surrogate solicitation, unrelated subjects) become negative keywords.
"""


def month_dates(month: str) -> tuple[str, str]:
    year, mon = (int(x) for x in month.split("-"))
    return f"{month}-01", f"{month}-{cal.monthrange(year, mon)[1]:02d}"


def _is_local(k: dict[str, Any]) -> bool:
    return k.get("label") in {"Local", "Location"} or k.get("group") == "local"


def _compact_keywords(keywords: list[dict[str, Any]], limit: int = LIMITS["keywords"]) -> list[str]:
    keywords = [k for k in keywords if k.get("keyword") and k.get("brand") != "competitor"]  # never rival brands
    local = [k for k in keywords if _is_local(k)]
    rest = [k for k in keywords if not _is_local(k)]
    rest.sort(key=lambda k: (k.get("position", 99), len(k.get("keyword", ""))))
    picked = (local + rest)[:limit]
    return [f"{k['keyword']} [{k.get('label') or k.get('intent', '')}]" for k in picked]


def _norm(keyword: str) -> str:
    """Keyword text without its [label], Google Ads match-type symbols, case or extra spaces."""
    keyword = re.sub(r"\s\[[^\]]*\]$", "", keyword.strip())
    return " ".join(re.sub(r'[\[\]"+]', " ", keyword).lower().split())


def pick_trend_keywords(keywords: list[dict[str, Any]], seed: str = "", limit: int = TRENDS_KEYWORDS) -> list[str]:
    """Short head terms worth a Trends lookup: no brands, questions or local phrases (Trends has no data there)."""
    pool = [k for k in keywords if k.get("keyword") and not k.get("brand") and not _is_local(k)
            and k.get("group") != "questions" and len(k["keyword"].split()) <= 3]
    pool.sort(key=lambda k: (len(k["keyword"].split()), k.get("position", 99), len(k["keyword"])))
    picked: list[str] = []
    for term in [seed, *(k["keyword"] for k in pool)]:
        term = " ".join(term.lower().split())
        if term and term not in picked:
            picked.append(term)
        if len(picked) == limit:
            break
    return picked


def compact_trends(trends: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Per keyword: direction, change_pct and the rising related searches (what Claude needs, nothing more)."""
    if not trends:
        return []
    related = trends.get("related", {})
    out = []
    for s in trends.get("summary", []):
        if s.get("direction") in (None, "no data"):
            continue
        rising = related.get(s["keyword"], {}).get("rising", [])[:LIMITS["rising_related"]]
        out.append({"keyword": s["keyword"], "direction": s["direction"], "change_pct": s.get("change_pct"),
                    "rising_related_searches": [f"{r['query']} ({r.get('value') or '?'})"
                                                for r in rising if r.get("query")]})
    return out


async def campaign_trends(keywords: list[dict[str, Any]], state: str | None, seed: str = "",
                          transport: httpx.AsyncBaseTransport | None = None
                          ) -> tuple[dict[str, Any] | None, str | None]:
    """Google Trends for the top keywords. Never raises: (result, None) or (None, why it is missing)."""
    terms = pick_trend_keywords(keywords, seed)
    if not terms:
        return None, "no short keywords to look up"
    try:
        result = await google_trends(terms, state, TRENDS_TIMEFRAME, transport=transport)
    except TrendsError as exc:
        log.warning("campaign planned without Google Trends: %s", exc)
        return None, str(exc)
    if not compact_trends(result):
        return None, f"no Google Trends data for these keywords in {state or 'India'}"
    return result, None


def compact_social(social: dict[str, Any] | None, questions: list[str] | None = None) -> dict[str, Any]:
    """Social listening insights within the request limits; {} when there is nothing to use."""
    social = social or {}
    out = {
        "run": social.get("query") or "", "run_at": social.get("run_at") or "",
        "open_questions": list(social.get("open_queries") or questions or [])[:LIMITS["questions"]],
        "pain_points": list(social.get("pain_points") or [])[:LIMITS["pain_points"]],
        "trending_topics": list(social.get("trending_topics") or [])[:LIMITS["trending_topics"]],
        "sentiment": social.get("sentiment") or None,
    }
    return out if out["open_questions"] or out["pain_points"] or out["trending_topics"] else {}


def check_sources(plan: dict[str, Any], keyword_list: list[str], trends: list[dict[str, Any]],
                  social: dict[str, Any], trends_error: str | None = None) -> list[dict[str, str]]:
    """Did the plan use the data it was given? Class C greeting slots are exempt (wishes only under NMC)."""
    known = {_norm(k) for k in keyword_list}
    problems: list[dict[str, str]] = []
    items, groups = plan.get("items", []), plan.get("ad_groups", [])
    for it in items:
        if it.get("nmc_class") == "C":
            continue
        where = f"{it.get('date', '')} {it.get('title', '')}".strip()
        kw, bf = it.get("target_keyword", ""), it.get("built_from") or {}
        if known and _norm(kw) not in known:
            problems.append({"where": where, "issue": "target keyword is not in the keyword research",
                             "text": kw or "(none)"})
        if (trends or social) and not bf.get("trend_signal") and not bf.get("social_insight"):
            problems.append({"where": where, "issue": "built on neither a Trends signal nor a social insight",
                             "text": it.get("theme", "")})
    for g in groups:
        for kw in g.get("keywords", []):
            if known and _norm(kw) not in known:
                problems.append({"where": f"{g.get('name', 'Ad group')} keywords",
                                 "issue": "keyword is not in the keyword research", "text": kw})

    built = [x.get("built_from") or {} for x in items + groups]
    notes = []
    if not keyword_list:
        notes.append("No keyword research was available, so target keywords are unchecked.")
    elif not any(b.get("keyword_source") for b in built):
        notes.append("Keyword research was sent but no slot says it used it.")
    if not trends:
        notes.append(f"Google Trends was not used ({trends_error or 'no data'}). Timing is not based on search trends.")
    elif not any(b.get("trend_signal") for b in built):
        notes.append("Google Trends data was sent but no slot uses it.")
    if not social:
        notes.append("No social listening run was used, so angles are not based on real audience questions.")
    elif not any(b.get("social_insight") for b in built):
        notes.append("Social listening insights were sent but no slot uses them.")
    return [{"where": "Plan", "issue": n, "text": ""} for n in notes] + problems


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
                         client: anthropic.AsyncAnthropic | None = None, *, keyword_origin: str = "auto",
                         trends: dict[str, Any] | None = None, trends_error: str | None = None,
                         social: dict[str, Any] | None = None) -> dict[str, Any]:
    if channel not in CAMPAIGN_CHANNELS:
        raise ValueError(f"unknown channel {channel}")
    if client is None:
        if not settings.anthropic_api_key:
            raise ScriptGenError("Set ANTHROPIC_API_KEY in .env (and restart the server) to build campaigns.")
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    info = CAMPAIGN_CHANNELS[channel]
    start, end = month_dates(month)
    weeks = 5 if int(end[-2:]) > 28 else 4
    keyword_list = _compact_keywords(keywords)
    trend_rows = compact_trends(trends)
    social_in = compact_social(social, questions)
    if not trend_rows:
        trends_error = trends_error or "no Google Trends data"
    request = {
        "institution_name": settings.brand_name or "[INSTITUTION NAME]",
        "topic": topic, "territory": territory.to_dict(), "month": month, "date_range": [start, end],
        "channel": channel, "channel_label": info["label"], "formats": info["formats"],
        "slots": per_week * weeks if channel != "google_ads" else weeks,
        "regional_research": regional.get("brief", {}),
        "keyword_research": keyword_list,
        "keyword_origin": "Keyword Studio results" if keyword_origin == "studio" else "automatic local search",
        "google_trends": {"geo": trends.get("geo"), "period": "last 3 months", "keywords": trend_rows,
                          "note": "Relative interest; change_pct compares the first and last month."}
                         if trend_rows and trends else {"unavailable": trends_error},
        "social_listening": social_in or {"unavailable": "no social listening run was used"},
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
    copy = [{k: v for k, v in i.items() if k != "built_from"} for i in plan["items"]]  # quoted data, not copy
    return {
        "territory": territory.to_dict(), "month": month, "channel": channel, "channel_label": info["label"],
        "plan": plan,
        "auto_check": post_check({"items": copy, "strategy": plan["strategy"]}),
        "ads_check": check_ads(plan.get("ad_groups", [])),
        "source_check": check_sources(plan, keyword_list, trend_rows, social_in, trends_error),
        "data_used": {
            "keywords": len(keyword_list), "keyword_origin": keyword_origin,
            "trends": len(trend_rows), "trends_error": None if trend_rows else trends_error,
            "trends_geo": trends.get("geo") if trend_rows and trends else None,
            "social_questions": len(social_in.get("open_questions", [])),
            "pain_points": len(social_in.get("pain_points", [])),
            "trending_topics": len(social_in.get("trending_topics", [])),
            "social_run": social_in.get("run") or None, "social_run_at": social_in.get("run_at") or None,
        },
        "model": response.model,
        "usage": {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
    }
