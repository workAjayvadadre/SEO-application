"""Deliverable brief for one calendar item, planned by Claude under the NMC advertising rules.

The marketing team writes the final video, blog or post. The brief tells them what it must achieve:
objective, hook options (also in the regional language), key messages, the audience questions to answer,
an outline (scene beats for video, sections for a blog), must-include / avoid lists, visual direction
(with the mandatory AI source mark when visuals are AI-made), caption direction, the only CTA NMC
allows, sources and KPIs, plus a compliance checklist. A deterministic claim check
(`regional.nmc_flags`) then re-scans the copy-like fields, so a slip is surfaced even when the
model marks itself compliant.

SYSTEM_PROMPT holds the NMC rules and is shared with the campaign planner (campaign.py).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from .config import Settings
from .regional import events_for, languages_for, nmc_flags, resolve_place

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """You plan healthcare marketing content for a fertility / IVF institution in India.
Everything you write must comply with the NMC "Guidelines on Ethical Advertising and Public Communication
by Hospitals/Medical Institutions and Registered Medical Practitioners", Public Notice
No. R-13014/01/2024-Ethics dated 06-10-2026 (in force immediately). Compliance outranks creativity and
the keyword you were given. If the keyword invites a prohibited claim, keep the topic and write the
compliant version; say what you changed in compliance.notes.

Allowed content classes (use only these):
- A: public-health education / awareness, no promotion of the practice (Cl. 5.1, 8.2 iii-iv)
- B: factual institutional information: name, location, services, facilities, charges (Cl. 8.3 i, 9.1)
- C: greeting for a festival or a day of national / medical importance with no professional-gain
  element: no offer, CTA, service list or booking contact (Cl. 8.2 v)
An AI-generated promotional campaign for commercial interest is prohibited (Cl. 7.2), so never write
class F (promotional) content.

Never write (each is a RED violation):
- Superlatives / comparatives: best, No.1, leading, top, most trusted, highest, unmatched, guaranteed,
  100%, painless, miracle (8.1 iii, ix). Rankings or awards without verifiable details.
- Success rates, pregnancy or live-birth %, patient or baby counts, outcome promises, concealed risks (8.1 iii, vi).
- Testimonials, patient stories or quotes, reviews, before/after, celebrity or influencer endorsement,
  "success baby" imagery (6.2; 8.1 v, xi).
- Offers, discounts, free consultations, EMI as an inducement, contests, "limited period" (8.1 x).
- Fear or urgency: "biological clock is ticking", "before it's too late" (8.1 i).
- Doctor-centric booking CTAs or self-promotion of an individual doctor (8.1 ii, 9.2). Name no doctor
  unless supplied; where a doctor appears, write the disclosure line
  "Dr [NAME], [QUALIFICATIONS], Reg. No. [SMC/NMR NO.] - verify on NMR/SMC register".
- Identifiable patient data, faces, scans, reports or embryo images of a real person (6.1, 6.4, 7.4; DPDP Act).
- AI-made likeness or voice of a real patient or doctor (7.2 b).
- Sex-selection or gender-preference cues, including pink-vs-blue (PC-PNDT Act).
- Soliciting egg / sperm / embryo donors or surrogates (ART Act 2021, Surrogacy Act 2021).
- Drug or remedy "cures" for infertility (Drugs & Magic Remedies Act 1954).
- "NMC approved" or "NMC certified".

Always:
- Facts must be general, accurate and verifiable; prefer "outcomes vary by individual; consult a
  qualified specialist". Never invent statistics, prices, registration numbers, awards, accreditation
  or service availability. Use [PLACEHOLDER] where the institution must supply a fact, and list every
  placeholder in compliance.human_approval.
- Every image brief: include the on-artwork AI source mark ("AI-generated image", or
  "AI-generated / AI-assisted content" when text or layout is also AI-made), text height at least
  ~2.5% of image height, high contrast; no real faces, no redrawn logos (use an "[Approved logo]" box),
  no deity imagery mixed with medical / pregnancy visuals, no pink/blue gender cues; give alt text
  under 125 characters.
- Captions of AI-assisted pieces must carry "AI-assisted content".
- Institution name: use the brand name given, else [INSTITUTION NAME].
- Festival greetings: wishes only, festive motifs (diya, rangoli, flowers, regional art), no medical
  visuals combined with deity imagery, correct festival name and spelling.
- Make it local: refer to the city / state, its language, culture, seasons and festivals where that
  is natural and respectful. No religious, caste, regional or gender stereotyping.
- Regional language: where you give hooks or lines in the regional language, use its own script and add
  an English back-translation.
- compliance.verdict is AMBER whenever any placeholder, statistic, price, accreditation or doctor detail
  needs human verification; GREEN only when nothing needs filling or checking. Never RED: rewrite instead.
"""

_S = {"type": "string"}
_STRS = {"type": "array", "items": _S}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["content_class", "title", "objective", "audience", "regional_angle", "language_plan",
                 "hook_options", "key_messages", "questions_to_answer", "outline", "must_include", "avoid",
                 "visual_direction", "caption_direction", "hashtags", "allowed_cta", "sources_to_cite",
                 "kpis", "compliance"],
    "properties": {
        "content_class": {"type": "string", "enum": ["A", "B", "C"]},
        "title": _S,
        "objective": _S,
        "audience": _S,
        "regional_angle": _S,
        "language_plan": _S,
        "hook_options": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["english", "regional", "back_translation"],
                      "properties": {"english": _S, "regional": _S, "back_translation": _S}},
        },
        "key_messages": _STRS,
        "questions_to_answer": _STRS,
        "outline": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["part", "purpose", "points", "approx_length"],
                      "properties": {"part": _S, "purpose": _S, "points": _STRS, "approx_length": _S}},
        },
        "must_include": _STRS,
        "avoid": _STRS,
        "visual_direction": {
            "type": "object", "additionalProperties": False,
            "required": ["canvas", "look_and_feel", "motifs", "exclusions", "ai_mark", "alt_text"],
            "properties": {"canvas": _S, "look_and_feel": _S, "motifs": _STRS, "exclusions": _STRS,
                           "ai_mark": _S, "alt_text": _S},
        },
        "caption_direction": _S,
        "hashtags": _STRS,
        "allowed_cta": _S,
        "sources_to_cite": _STRS,
        "kpis": _STRS,
        "compliance": {
            "type": "object", "additionalProperties": False,
            "required": ["verdict", "notes", "checklist", "human_approval"],
            "properties": {
                "verdict": {"type": "string", "enum": ["GREEN", "AMBER"]},
                "notes": _S,
                "checklist": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": False,
                              "required": ["check", "status", "clause"],
                              "properties": {"check": _S, "status": {"type": "string", "enum": ["Pass", "Flag", "N/A"]},
                                             "clause": _S}},
                },
                "human_approval": _STRS,
            },
        },
    },
}

_FORMAT_GUIDE = {
    "Reel": "Instagram Reel, 30-45 s, 9:16 (1080x1920). Outline = 5-8 scene beats with seconds; hook lands in "
            "the first 2 seconds. Visual direction for the cover frame.",
    "Short": "YouTube Short, under 60 s, 9:16. Outline = 5-8 scene beats with seconds. Thumbnail direction.",
    "Long video": "YouTube video, 6-10 min, 16:9. Outline = chapters with minutes; include a description and "
                  "chapter list in must_include. 1280x720 thumbnail direction.",
    "Carousel": "Instagram carousel, 6-8 slides at 1080x1350. Outline = one part per slide.",
    "Static post": "Single Instagram image 1080x1350. Outline = headline, supporting line, caption.",
    "Blog post": "Website article, 1,200-1,800 words. Outline = H2 sections with word counts, an FAQ section and "
                 "'When to consult a specialist'. Put a meta title (<60 chars) and meta description (<155 chars) "
                 "in must_include. Featured-image direction.",
    "Post": "Facebook post, 120-250 words. Outline = opening, 2-3 points, close. 1200x630 visual.",
    "Q&A post": "Facebook Q&A post, 150-250 words: the question and the points of a plain answer.",
    "Update post": "Google Business Profile update, 100-200 words, Class B factual information only. 1200x900 visual.",
    "Festive greeting": "Class C greeting, 1080x1350: wishes only. Regional festive motifs.",
    "Greeting": "Class C greeting, 1080x1350: wishes only.",
    "Awareness post": "Class A awareness carousel, 5-6 slides: facts with cited public sources (WHO, ICMR, MoHFW).",
}


class ScriptGenError(RuntimeError):
    """User-facing failure (missing key, refusal, truncated output, API error)."""


def build_request(item: dict[str, Any], *, month: str | None, region: str | None, country: str,
                  brand: str | None, language: str | None = None) -> str:
    place = resolve_place(region)
    india = country.lower() == "in"
    langs = languages_for(place.get("state")) if india else []
    lang = language or (langs[0] if langs else "English")
    events = events_for(month, place.get("state")) if (india and month) else []
    fmt = item.get("format", "")
    brief = {
        "channel": item.get("channel_label") or item.get("channel"),
        "format": fmt,
        "format_guide": _FORMAT_GUIDE.get(fmt, "Social post. One image brief."),
        "publish_date": item.get("date"),
        "working_title": item.get("title"),
        "target_keyword": item.get("keyword"),
        "intent": item.get("intent"),
        "funnel_stage": item.get("funnel"),
        "planned_hashtags": item.get("hashtags", []),
        "keyword_claim_flags": [f"{f['match']}: {f['issue']} ({f['clause']})" for f in nmc_flags(item.get("keyword", ""))],
        "event": item.get("event"),
        "institution_name": brand or "[INSTITUTION NAME]",
        "location": {"region_as_typed": region, **place, "country": country.upper()},
        "regional_language": lang,
        "other_local_languages": langs[1:],
        "festivals_and_days_this_month": [f"{e['date']} {e['name']}{' (verify date)' if e['verify'] else ''}"
                                          for e in events],
    }
    return ("Write the deliverable brief for this calendar item: what the team's video, post or article must "
            "achieve and contain. Do not write the finished piece. Follow the format guide and return the JSON "
            "object.\n\n" + json.dumps(brief, ensure_ascii=False, indent=2))


# Fields that list banned wording on purpose ("avoid: 'best'", negative keywords) are not checked.
_NOT_COPY = frozenset({"compliance", "exclusions", "avoid", "negative_keywords", "nmc_notes", "sensitivities"})


def _collect_text(node: Any, skip: frozenset[str] = _NOT_COPY) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [t for x in node for t in _collect_text(x, skip)]
    if isinstance(node, dict):
        return [t for k, v in node.items() if k not in skip for t in _collect_text(v, skip)]
    return []


def post_check(content: dict[str, Any]) -> list[dict[str, str]]:
    """Claim patterns found anywhere in the generated copy (compliance notes and exclusion lists skipped)."""
    hits: dict[tuple[str, str], dict[str, str]] = {}
    for text in _collect_text(content):
        for flag in nmc_flags(text):
            hits.setdefault((flag["issue"], flag["match"].lower()),
                            {**flag, "context": text[:160]})
    return list(hits.values())


async def generate_content(settings: Settings, item: dict[str, Any], *, month: str | None, region: str | None,
                           country: str = "in", language: str | None = None,
                           client: anthropic.AsyncAnthropic | None = None) -> dict[str, Any]:
    if client is None:
        if not settings.anthropic_api_key:
            raise ScriptGenError("Set ANTHROPIC_API_KEY in .env (and restart the server) to generate scripts.")
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    prompt = build_request(item, month=month, region=region, country=country, brand=settings.brand_name,
                           language=language)
    try:
        response = await client.beta.messages.create(
            model=settings.content_model,
            max_tokens=16000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            cache_control={"type": "ephemeral"},
            system=SYSTEM_PROMPT,
            output_config={"effort": settings.content_effort,
                           "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
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
        raise ScriptGenError("The model declined this request. Rephrase the topic or keyword.")
    if response.stop_reason == "max_tokens":
        raise ScriptGenError("The content was cut off (too long). Try again, or pick a shorter format.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        content = json.loads(text)
    except ValueError as exc:
        log.warning("unparseable content (request %s)", getattr(response, "_request_id", "?"))
        raise ScriptGenError("The model returned malformed content. Try again.") from exc

    hits = post_check(content)
    if hits and content.get("compliance", {}).get("verdict") == "GREEN":
        content["compliance"]["verdict"] = "AMBER"
    return {"content": content, "auto_check": hits, "model": response.model,
            "usage": {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}}
