"""Turns keyword research into a monthly, channel-wise content calendar.

Each keyword gets a fit score per channel. Fit comes from what the keyword reads like: "how does X work"
suits a video, "X vs Y" a blog comparison, "signs of X" an Instagram carousel, "X near me" a Google
Business Profile post. The fit score is multiplied by a popularity proxy: Google lists more-searched
completions first. Slots are filled round-robin across topic clusters, so each week covers several
topics, and no keyword is used twice.
"""

from __future__ import annotations

import calendar
import json
from pathlib import Path
import re
from collections import defaultdict
from datetime import date
from typing import Any

from .analysis import STOPWORDS
from .regional import events_for, languages_for, nmc_flags, resolve_place, safe_angle

CHANNELS: dict[str, dict[str, Any]] = {
    "blog": {"label": "Website blog", "formats": ["Blog post"]},
    "youtube": {"label": "YouTube", "formats": ["Long video", "Short"]},
    "instagram": {"label": "Instagram", "formats": ["Reel", "Carousel", "Reel", "Static post"]},
    "facebook": {"label": "Facebook", "formats": ["Post", "Q&A post"]},
    "gbp": {"label": "Google Business Profile", "formats": ["Update post"]},
}
DEFAULT_CADENCE = {"blog": 3, "instagram": 4, "youtube": 2, "facebook": 2, "gbp": 1}
# Most specific channels pick first so the flexible blog doesn't take every strong keyword.
_PICK_ORDER = ("gbp", "youtube", "instagram", "facebook", "blog")

# Weekday patterns (Mon=0) for N posts per week, spread out rather than bunched.
_WEEKDAYS = {1: (1,), 2: (1, 4), 3: (0, 2, 4), 4: (0, 1, 3, 5), 5: (0, 1, 2, 3, 4),
             6: (0, 1, 2, 3, 4, 5), 7: (0, 1, 2, 3, 4, 5, 6)}

_Q = r"^(what|why|how|when|where|which|who|is|are|can|does|do|should|will)\b"
_FIT_RULES: dict[str, list[tuple[re.Pattern[str], float]]] = {
    "blog": [(re.compile(_Q), 3.0), (re.compile(r"\b(vs|versus|or)\b"), 2.5),
             (re.compile(r"\b(cost|price|charges|fees|rate|rates)\b"), 2.5),
             (re.compile(r"\b(guide|process|steps|treatment|procedure|benefits|side effects|risks|success)\b"), 2.0),
             (re.compile(r"\b(best|top)\b"), 1.5)],
    "youtube": [(re.compile(r"\b(how|process|steps|procedure|explained|work|works|journey|experience|vlog|"
                            r"review|reviews|vs|day|story)\b"), 3.0),
                (re.compile(r"^(what|why)\b"), 1.5)],
    "instagram": [(re.compile(r"\b(tips|signs|symptoms|myths?|facts|diet|food|foods|yoga|exercise|success|"
                              r"stories|story|journey|motivation|quotes|before|after|mistakes|care|food)\b"), 3.0),
                  (re.compile(r"^(can|is|does|should)\b"), 1.5)],
    "facebook": [(re.compile(r"\b(experience|success|story|stories|cost|near me|best|doctor|hospital|clinic|"
                             r"centre|center|reviews?)\b"), 2.5),
                 (re.compile(r"^(can|is|does|should|which|what)\b"), 1.5)],
    "gbp": [(re.compile(r"\b(near me|best|cost|price|hospital|clinic|centre|center|doctor)\b"), 3.0)],
}
_ACRONYMS = {"ivf", "iui", "icsi", "pcos", "pcod", "amh", "hsg", "fet", "imsi", "pgt", "pgs", "tsh", "bmi",
             "seo", "emi", "ai", "usa", "uk", "uae", "ncr", "diy", "faq"}
_WORD = re.compile(r"[a-z0-9]+")


def smart_title(text: str) -> str:
    def fix(word: str) -> str:
        bare = word.strip("?,.!")
        if bare in _ACRONYMS:
            return word.replace(bare, bare.upper())
        if bare in {"vs", "or", "and", "for", "in", "of", "to", "with", "the", "a", "an", "on", "at", "by"}:
            return word
        return word[:1].upper() + word[1:]
    words = text.split()
    titled = [fix(w) for w in words]
    if titled:
        titled[0] = titled[0][:1].upper() + titled[0][1:]
    return " ".join(titled)


def suggest_title(channel: str, fmt: str, keyword: str) -> str:
    title = smart_title(keyword)
    is_question = re.match(_Q, keyword) is not None
    parts = re.split(r"\s+(?:vs|versus)\s+", keyword, maxsplit=1)
    if channel == "blog":
        if len(parts) == 2:
            return f"{smart_title(parts[0])} vs {smart_title(parts[1])}: Key Differences Explained"
        if is_question:
            return f"{title}?"
        if re.search(r"\b(cost|price|charges|fees)\b", keyword):
            return f"{title}: Complete Cost Breakdown"
        return f"{title}: A Complete Guide"
    if channel == "youtube":
        if fmt == "Short":
            return f"{title}{'?' if is_question else ''} (in 60 seconds)"
        return f"{title}? Expert Explains" if is_question else f"{title}: Explained Step by Step"
    if channel == "instagram":
        hooks = {"Reel": "Reel hook: ", "Carousel": "Carousel: ", "Static post": ""}
        return f"{hooks.get(fmt, '')}{title}{'?' if is_question else ''}"
    if channel == "facebook":
        return f"Ask the expert: {title}?" if fmt == "Q&A post" else f"{title}{'?' if is_question else ''}"
    return f"{title}: Clinic Information"


def hashtags(keyword: str, seed: str, region: str | None, limit: int = 8) -> list[str]:
    tags = ["#" + "".join(_WORD.findall(seed))]
    if region:
        tags.append("#" + "".join(_WORD.findall(seed)) + "".join(_WORD.findall(region.lower())))
    tags += ["#" + w for w in _WORD.findall(keyword)
             if len(w) > 2 and w not in STOPWORDS and w not in _CLAIM_WORDS]
    return list(dict.fromkeys(tags))[:limit]


# Words that must not become hashtags (NMC 8.1: superlatives, outcome claims, inducements).
_CLAIM_WORDS = frozenset("best top leading highest lowest cheapest famous success rate rates guaranteed "
                         "guarantee painless miracle free offer discount number".split())

_LANGUAGE_WORDS = re.compile(r"\b(hindi|telugu|tamil|marathi|kannada|malayalam|bengali|bangla|gujarati|punjabi|"
                             r"urdu|odia|english)\b")
# YouTube autocomplete is often ungrammatical ("what ivf treatment pregnancy"): fine as a video idea,
# poor as a blog headline. These channels only draw from Google web suggestions.
_GOOGLE_ONLY = {"blog", "facebook", "gbp"}


def channel_fit(channel: str, keyword: str, source: str, group: str) -> float:
    if channel in _GOOGLE_ONLY and source == "youtube":
        return 0.0
    fit = sum(weight for pattern, weight in _FIT_RULES[channel] if pattern.search(keyword))
    if _LANGUAGE_WORDS.search(keyword):
        # "ivf process in hindi" is a great regional-language video, a poor blog or GBP post.
        return fit + 2.0 if channel == "youtube" else 0.1
    words = len(keyword.split())
    if channel == "blog" and words >= 4:
        fit += 1.0
    if channel == "instagram" and words <= 5:
        fit += 1.0
    if channel == "youtube" and source == "youtube":
        fit += 2.5
    if channel in {"gbp", "facebook"} and group == "local":
        fit += 2.5
    if channel == "gbp" and group != "local" and "near" not in keyword:
        fit *= 0.3  # GBP posts should be local
    return fit or 0.5  # everything stays eligible so a thin month still fills


def popularity(position: int) -> float:
    """Autocomplete rank is the only free popularity signal: #1 → 2.0, #10 → 1.1."""
    return round(1.0 + (11 - min(max(position, 1), 10)) / 10, 2)


# Shifts each channel's weekday pattern so channels don't all land on the same days.
_DAY_OFFSET = {"blog": 0, "instagram": 1, "youtube": 3, "facebook": 2, "gbp": 4}


def month_slots(month: str, per_week: int, offset: int = 0) -> list[date]:
    year, mon = (int(x) for x in month.split("-"))
    weekdays = {(w + offset) % 7 for w in _WEEKDAYS[max(1, min(per_week, 7))]}
    days = calendar.monthrange(year, mon)[1]
    return [date(year, mon, d) for d in range(1, days + 1) if date(year, mon, d).weekday() in weekdays]


_BASE_CITIES = frozenset(
    "delhi mumbai bangalore bengaluru hyderabad chennai kolkata pune ahmedabad jaipur lucknow chandigarh indore "
    "patna bhopal surat nagpur kanpur vadodara ludhiana agra nashik varanasi udaipur jodhpur kota ajmer bikaner "
    "raipur ranchi guwahati bhubaneswar coimbatore kochi madurai mysore mysuru dehradun noida gurgaon gurugram "
    "ghaziabad faridabad thane amritsar jammu srinagar siliguri vijayawada visakhapatnam trivandrum "
    "thiruvananthapuram mangalore mangaluru aurangabad rajkot gwalior jabalpur prayagraj allahabad meerut "
    "bareilly gorakhpur hubli hubballi".split())
_REGIONS_FILE = Path(__file__).resolve().parent.parent / "ui" / "regions.json"


def _load_cities() -> frozenset[str]:
    """Single-word city names from ui/regions.json (multi-word ones would match common words like 'new')."""
    try:
        data = json.loads(_REGIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _BASE_CITIES
    names = {c.lower() for cities in data.get("cities", {}).values() for c in cities if " " not in c}
    ambiguous = {"along", "anand", "sagar", "banda", "puri", "panna", "pala", "mandi", "durg", "chas", "fort",
                 "camp", "mahe", "kota", "pali", "tura", "rajgarh", "mansa"} | set(STOPWORDS)
    return _BASE_CITIES | frozenset(n for n in names if len(n) > 3 and n not in ambiguous)


_INDIAN_CITIES = _load_cities()


def is_other_city(keyword: str, region: str | None) -> bool:
    """With a region chosen, 'ivf cost in mumbai' is someone else's local search, not yours."""
    if not region:
        return False
    own = set(_WORD.findall(region.lower()))
    return any(w in _INDIAN_CITIES and w not in own for w in _WORD.findall(keyword))


def _topic_map(clusters: list[dict[str, Any]]) -> dict[str, str]:
    return {kw: c["topic"] for c in clusters for kw in c["keywords"]}


_EVENT_FORMATS = {
    "festival": ("Festive greeting", "Class C greeting: wishes only, no offer, CTA, services or doctor promotion"),
    "national": ("Greeting", "Class C greeting: wishes only, no offer, CTA, services or doctor promotion"),
    "health": ("Awareness post", "Class A public-health awareness: facts and sources, no promotion of the practice"),
}
_EVENT_CHANNELS = ("instagram", "facebook")


def _event_items(month: str, state: str | None, region: str | None, cadence: dict[str, int],
                 india: bool) -> list[dict[str, Any]]:
    if not india:
        return []
    social = [c for c in _EVENT_CHANNELS if cadence.get(c)]
    items = []
    for ev in events_for(month, state):
        fmt, rule = _EVENT_FORMATS[ev["kind"]]
        # Greetings belong on social; awareness days can fall back to the blog.
        channels = social or (["blog"] if ev["kind"] == "health" and cadence.get("blog") else [])
        title = (f"Wishing you a happy and healthy {ev['name']}" if ev["kind"] != "health"
                 else f"{ev['name']}: facts worth knowing")
        for channel in channels:
            items.append({
                "date": ev["date"], "channel": channel, "channel_label": CHANNELS[channel]["label"],
                "format": fmt, "keyword": ev["name"], "title": title, "topic": "festivals & observances",
                "intent": "informational", "funnel": "awareness", "popularity": 0, "source": "calendar",
                "kind": "event", "event": {**ev, "rule": rule},
                "hashtags": [h for h in ["#" + "".join(_WORD.findall(ev["name"].lower())),
                                         "#" + "".join(_WORD.findall(region.lower())) if region else None,
                                         "#FertilityAwareness" if ev["kind"] == "health" else None] if h],
            })
    return items


def _local_words(region: str | None, place: dict[str, str | None]) -> set[str]:
    words: set[str] = set()
    for part in (region, place.get("area"), place.get("city")):
        words |= {w for w in _WORD.findall((part or "").lower()) if len(w) > 2 and w not in STOPWORDS}
    return words


def build_calendar(month: str, seed: str, google: dict[str, Any], youtube: dict[str, Any] | None,
                   cadence: dict[str, int], region: str | None = None, country: str = "in") -> dict[str, Any]:
    place = resolve_place(region)
    local = _local_words(region, place)
    topics = _topic_map(google.get("clusters", []))
    ideas: list[dict[str, Any]] = [{**k, "source": "google"} for k in google.get("keywords", [])]
    if youtube:
        known = {i["keyword"] for i in ideas}
        ideas += [{**k, "source": "youtube"} for k in youtube.get("keywords", []) if k["keyword"] not in known]
    ideas = [i for i in ideas if not is_other_city(i["keyword"], region)]
    for idea in ideas:
        idea["topic"] = topics.get(idea["keyword"], "video ideas" if idea["source"] == "youtube" else "other")
        idea["popularity"] = popularity(idea["position"])
        idea["local"] = bool(local & set(_WORD.findall(idea["keyword"]))) or idea.get("group") == "local"

    used: set[str] = set()
    items: list[dict[str, Any]] = []
    for channel in _PICK_ORDER:
        per_week = int(cadence.get(channel) or 0)
        if per_week <= 0:
            continue
        slots = month_slots(month, per_week, _DAY_OFFSET.get(channel, 0))
        picks = _pick(channel, ideas, len(slots), used)
        formats = CHANNELS[channel]["formats"]
        for i, (day, idea) in enumerate(zip(slots, picks)):
            fmt = formats[i % len(formats)]
            flags = nmc_flags(idea["keyword"])
            angle = safe_angle(idea["keyword"], place.get("city") or region, salt=channel + fmt)
            items.append({
                "date": day.isoformat(), "channel": channel, "channel_label": CHANNELS[channel]["label"],
                "format": fmt, "keyword": idea["keyword"],
                "title": angle or suggest_title(channel, fmt, idea["keyword"]),
                "topic": idea["topic"], "intent": idea["intent"], "funnel": idea["funnel"],
                "popularity": idea["popularity"], "source": idea["source"], "kind": "keyword",
                "local": idea["local"],
                "nmc": {"flags": flags, "status": "reframed" if flags else "ok"},
                "hashtags": hashtags(idea["keyword"], seed, region) if channel in {"instagram", "youtube"} else [],
            })

    items += _event_items(month, place.get("state"), region, cadence, country.lower() == "in")
    items.sort(key=lambda it: (it["date"], it.get("kind") != "event", _PICK_ORDER.index(it["channel"])))
    summary = defaultdict(int)
    for item in items:
        summary[item["channel"]] += 1
    events = [] if country.lower() != "in" else events_for(month, place.get("state"))
    return {"month": month, "seed": seed, "region": region, "cadence": cadence,
            "place": place, "languages": languages_for(place.get("state")) if country.lower() == "in" else [],
            "events": events,
            "local_items": sum(1 for it in items if it.get("local")),
            "nmc_reframed": sum(1 for it in items if it.get("nmc", {}).get("flags")),
            "channels": {k: v["label"] for k, v in CHANNELS.items()},
            "summary": dict(summary), "total_items": len(items), "items": items}


_LOCAL_BOOST = 2.5  # a regional plan should lead with searches people make in that region
_CLAIM_PENALTY = 0.4  # keywords that invite NMC-prohibited claims make weaker content; use them sparingly


def _pick(channel: str, ideas: list[dict[str, Any]], count: int, used: set[str]) -> list[dict[str, Any]]:
    """Best-scoring keywords, rotated across topic clusters. Unused keywords are preferred."""
    fits = {i["keyword"]: channel_fit(channel, i["keyword"], i["source"], i["group"]) for i in ideas}
    # Competitor-brand searches are research signals only: never schedule content around them.
    scored = sorted((i for i in ideas if fits[i["keyword"]] > 0 and i.get("brand") != "competitor"),
                    key=lambda i: fits[i["keyword"]] * i["popularity"] * (_LOCAL_BOOST if i.get("local") else 1.0)
                    * (_CLAIM_PENALTY if nmc_flags(i["keyword"]) else 1.0),
                    reverse=True)
    picks: list[dict[str, Any]] = []
    for allow_reuse in (False, True):
        queues: dict[str, list[dict[str, Any]]] = defaultdict(list)
        chosen = {p["keyword"] for p in picks}
        for idea in scored:
            if idea["keyword"] in chosen or (not allow_reuse and idea["keyword"] in used):
                continue
            queues[idea["topic"]].append(idea)
        order = list(queues)  # insertion order = topic of the best-scoring keyword first
        while len(picks) < count and any(queues.values()):
            for topic in order:
                if queues[topic] and len(picks) < count:
                    idea = queues[topic].pop(0)
                    picks.append(idea)
                    used.add(idea["keyword"])
        if len(picks) >= count:
            break
    return picks
