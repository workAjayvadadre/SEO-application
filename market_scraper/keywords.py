"""Keyword idea engine (the Answer Socrates / AnswerThePublic method).

A seed keyword is expanded with question words, prepositions, comparison words and A–Z letters.
Each variant goes to Google Autocomplete, which returns what people actually type. Results are
de-duplicated, filtered for relevance, tagged with search intent and funnel stage, and clustered
into topics.

Autocomplete source: Google's public suggest endpoint (free). If a call fails and a SERP key is set,
that call is retried through Serper/SerpApi autocomplete (paid, 1 credit per call).
"""

from __future__ import annotations

import asyncio
import json
import re
import string
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from typing import Any

import httpx

from .analysis import STOPWORDS
from .config import Settings
from .http import FetchError, HttpClient
from .serp import SerpClient
from .utils import utc_now_iso

GOOGLE_SUGGEST = "https://suggestqueries.google.com/complete/search"

# Same expansion set as AnswerSocrates' export: (modifier label, query pattern), {s} = seed.
def _pre(*words: str) -> tuple[tuple[str, str], ...]:
    return tuple((w, f"{w} {{s}}") for w in words)


def _post(*words: str) -> tuple[tuple[str, str], ...]:
    return tuple((w, f"{{s}} {w}") for w in words)


GROUPS: dict[str, tuple[tuple[str, str], ...]] = {
    "query": (("{seed}", "{s}"), ("* {seed}", "{s} ")),
    "questions": _pre("are", "can i", "can", "how do", "how", "should i", "should", "what", "when", "where are",
                      "where is", "where", "which", "who", "why", "will", "is", "does"),
    "past": _pre("did", "was"),
    "prepositions": _post("can", "for", "is", "near", "to", "with", "without"),
    "comparisons": _post("and", "compared", "like", "or", "similar", "versus", "vs")
                   + _pre("best", "better", "top")
                   + (("best * for", "best {s} for"), ("best * with", "best {s} with"))
                   + _post("review", "reviews", "rating", "alternative", "similar to", "price", "cost", "cheap",
                           "expensive", "recommended"),
    "alphabetical": tuple((c, f"{{s}} {c}") for c in string.ascii_lowercase)
                    + tuple((c, f"{c} {{s}}") for c in string.ascii_lowercase),
}
ALL_GROUPS: tuple[str, ...] = tuple(GROUPS)
# Connector words that should never name a topic cluster (commercial words like "cost" or "best" can).
_MODIFIER_WORDS = set("what why how when where which who are can will is does should did was for with without "
                      "near to vs versus or and like compared similar".split())

# First matching rule wins, so the most purchase-ready intent is checked first.
_INTENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("transactional", re.compile(
        r"\b(buy|buying|price|prices|pricing|cost|costs|cheap|cheapest|affordable|deal|deals|discount|"
        r"coupon|sale|order|near me|shop|store|rent|rental|hire|book|booking|quote|delivery|emi|offer)\b")),
    ("commercial", re.compile(
        r"\b(best|top|vs|versus|review|reviews|compare|comparison|alternative|alternatives|rated|"
        r"worth it|pros and cons|recommended|brands?)\b")),
    ("navigational", re.compile(
        r"\b(login|log in|sign in|website|official|app|contact|customer care|customer service|"
        r"phone number|address|hours|helpline)\b")),
)
FUNNEL = {"informational": "awareness", "commercial": "consideration",
          "transactional": "decision", "navigational": "decision"}
_TOKEN = re.compile(r"[a-z0-9][a-z0-9'+\-]*")
# Hinglish filler ("ivf kya hai", "ivf kaise hota hai") must not become topic names.
_HINGLISH_STOPWORDS = frozenset(
    "hai hain kya ka ki ke ko me mein se par aur kaise kitna kitne kitni hota hoti hote kab kyu kyon "
    "liye wala wali bhi nahi karna kare karein hindi".split())


@dataclass(frozen=True, slots=True)
class KeywordRequest:
    keyword: str
    source: str = "google"  # google | youtube
    country: str = "us"
    language: str = "en"
    include_serp: bool = False  # People Also Ask + related searches (1 SERP credit)
    groups: tuple[str, ...] = ALL_GROUPS
    region: str | None = None  # e.g. "Jaipur" or "Maharashtra": adds a "local" group of searches


# Local intent patterns ({s} = seed, {r} = region). The label is what the UI shows as the modifier.
LOCAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("in {r}", "{s} in {r}"), ("{r}", "{s} {r}"), ("best … in {r}", "best {s} in {r}"),
    ("cost in {r}", "{s} cost in {r}"), ("top … in {r}", "top {s} in {r}"), ("near me", "{s} near me"),
)


_PLACE_WORDS = re.compile(r"\b(india|indian|delhi|mumbai|bangalore|bengaluru|hyderabad|chennai|kolkata|pune|"
                          r"ahmedabad|jaipur|lucknow|abroad|usa|uk|dubai|city|state)\b")
_BOFU = re.compile(r"\b(cost|costs|price|prices|charges|fees|package|packages|cheap|cheapest|affordable|"
                   r"expensive|emi|insurance|loan|book|booking|appointment|clinic|centre|center|hospital|doctor|"
                   r"specialist|best|top|review|reviews|rating|recommended|near)\b")
_MOFU = re.compile(r"\b(vs|versus|or|compared|alternative|alternatives|similar|better|difference|"
                   r"pros and cons|which|types|options)\b")


def keyword_label(keyword: str, group: str, region_words: frozenset[str] = frozenset()) -> str:
    """AnswerSocrates-style label: Local, Location, BOFU, MOFU, Long Tail or TOFU."""
    words = set(keyword.split())
    if "near me" in keyword or group == "local" or (region_words and region_words & words):
        return "Local"
    if _PLACE_WORDS.search(keyword) or _is_city(words):
        return "Location"
    if _BOFU.search(keyword):
        return "BOFU"
    if _MOFU.search(keyword):
        return "MOFU"
    if len(keyword.split()) >= 7:
        return "Long Tail"
    return "TOFU"


def _is_city(words: set[str]) -> bool:
    try:
        from .planner import _INDIAN_CITIES  # lazy: planner imports this module
    except ImportError:  # pragma: no cover
        return False
    return bool(words & _INDIAN_CITIES)


# Fertility clinic brands people search for in India. Matched as whole words next to the keyword.
# Extend with COMPETITOR_TERMS in .env. Used to label, never to target: competitor names can't appear in
# our content (NMC 8.1 comparisons) and are kept out of the content calendar.
COMPETITOR_BRANDS = frozenset(
    "nova birla cloudnine milann oasis ferty9 morpheus medicover manipal apollo motherhood gaudium "
    "sitaram aashakiran ankuram nimaaya jaslok garbhagudi prashanth vardaan progenesis southend "
    "bhailal elaa kjk arihant crysta zydus fortis".split())


def brand_of(keyword: str, own: frozenset[str], competitors: frozenset[str]) -> str | None:
    words = set(_TOKEN.findall(keyword))
    if words & own:
        return "own"
    if words & competitors:
        return "competitor"
    return None


@dataclass(slots=True)
class KeywordIdea:
    keyword: str
    group: str
    modifier: str
    position: int  # rank within that autocomplete list (1 = Google's top suggestion)
    intent: str
    funnel: str
    label: str = "TOFU"
    brand: str | None = None  # "own" | "competitor" | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _Stats:
    sent: int = 0
    failed: int = 0
    serp_fallbacks: int = 0
    errors: list[str] = field(default_factory=list)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def classify_intent(keyword: str) -> str:
    for intent, pattern in _INTENT_RULES:
        if pattern.search(keyword):
            return intent
    return "informational"


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def is_relevant(suggestion: str, seed: str) -> bool:
    """Every meaningful seed word (singular/plural-insensitive) must appear in the suggestion."""
    seed_terms = {_stem(t) for t in _TOKEN.findall(seed) if t not in STOPWORDS} or {_stem(t) for t in _TOKEN.findall(seed)}
    found = {_stem(t) for t in _TOKEN.findall(suggestion)}
    return seed_terms <= found


def cluster_keywords(ideas: list[KeywordIdea], seed: str, min_size: int = 3,
                     max_clusters: int = 24) -> list[dict[str, Any]]:
    """Greedy topic clustering on the most frequent non-seed word in each keyword."""
    excluded = {_stem(t) for t in _TOKEN.findall(seed)} | _MODIFIER_WORDS | STOPWORDS | _HINGLISH_STOPWORDS
    surface: dict[str, Counter[str]] = defaultdict(Counter)
    terms_of: dict[str, set[str]] = {}
    for idea in ideas:
        terms = set()
        for token in _TOKEN.findall(idea.keyword):
            stem = _stem(token)
            if len(token) > 2 and stem not in excluded and token not in excluded and not token.isdigit():
                terms.add(stem)
                surface[stem][token] += 1
        terms_of[idea.keyword] = terms

    frequency = Counter(t for terms in terms_of.values() for t in terms)
    clusters: list[dict[str, Any]] = []
    assigned: set[str] = set()
    for term, count in frequency.most_common():
        if count < min_size or len(clusters) >= max_clusters:
            break
        members = [i.keyword for i in ideas if i.keyword not in assigned and term in terms_of[i.keyword]]
        if len(members) >= min_size:
            clusters.append({"topic": surface[term].most_common(1)[0][0], "size": len(members), "keywords": members})
            assigned.update(members)
    clusters.sort(key=lambda c: c["size"], reverse=True)
    rest = [i.keyword for i in ideas if i.keyword not in assigned]
    if rest:
        clusters.append({"topic": "other", "size": len(rest), "keywords": rest})
    return clusters


class KeywordEngine:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # Own client: Suggest needs faster pacing than page scraping, and fewer retries (50 calls/run).
        self.settings = replace(settings, requests_per_second_per_host=settings.keyword_rps,
                                max_retries=min(settings.max_retries, 2))
        self._transport = transport

    async def run(self, request: KeywordRequest) -> dict[str, Any]:
        seed = normalize(request.keyword)
        if not seed:
            raise ValueError("keyword must not be empty")
        unknown = set(request.groups) - set(GROUPS)
        if unknown:
            raise ValueError(f"unknown group(s): {', '.join(sorted(unknown))}")

        started = time.perf_counter()
        stats = _Stats()
        async with HttpClient(self.settings, transport=self._transport) as http:
            serp = SerpClient(self.settings, http)
            jobs = self._build_jobs(seed, request)
            serp_task = (asyncio.create_task(serp.search(seed)) if request.include_serp and serp.configured else None)
            suggestions = await asyncio.gather(*(self._suggest(http, serp, q, request, stats) for _, _, q in jobs))
            serp_result = None
            if serp_task:
                try:
                    serp_result = await serp_task
                except Exception as exc:  # optional extra; never fail the run for it
                    stats.errors.append(f"People Also Ask: {exc}")

        if stats.sent and stats.failed == stats.sent:
            raise FetchError(f"All {stats.sent} autocomplete requests failed: {stats.errors[0] if stats.errors else ''}")

        ideas, groups_out = self._collect_ideas(seed, jobs, suggestions)
        own = frozenset(t.lower() for t in self.settings.own_brand_terms)
        rivals = (COMPETITOR_BRANDS | {t.lower() for t in self.settings.competitor_terms}) - own
        for idea in ideas:
            idea.brand = brand_of(idea.keyword, own, frozenset(rivals))
        return {
            "keyword": seed,
            "source": request.source,
            "country": request.country,
            "language": request.language,
            "region": request.region,
            "generated_at": utc_now_iso(),
            "duration_seconds": round(time.perf_counter() - started, 2),
            "total_keywords": len(ideas),
            "requests": {"sent": stats.sent, "failed": stats.failed, "serp_fallbacks": stats.serp_fallbacks},
            "warnings": stats.errors[:10],
            "counts": {g: sum(len(m["keywords"]) for m in groups_out.get(g, []))
                       for g in (*request.groups, *(("local",) if request.region else ()))},
            "intents": dict(Counter(i.intent for i in ideas)),
            "groups": groups_out,
            "clusters": cluster_keywords(ideas, seed),
            "people_also_ask": [{"question": q["question"], "snippet": q.get("snippet"), "link": q.get("link")}
                                for q in (serp_result.people_also_ask if serp_result else [])],
            "related_searches": serp_result.related_searches if serp_result else [],
            "keywords": [i.to_dict() for i in ideas],
        }

    @staticmethod
    def _build_jobs(seed: str, request: KeywordRequest) -> list[tuple[str, str, str]]:
        """(group, modifier, query sent to autocomplete) for every expansion."""
        jobs: list[tuple[str, str, str]] = []
        for group in request.groups:
            for label, pattern in GROUPS[group]:
                jobs.append((group, label.replace("{seed}", seed), pattern.format(s=seed)))
        region = normalize(request.region or "")
        if region:
            jobs += [("local", label.replace("{r}", region), pattern.format(s=seed, r=region))
                     for label, pattern in LOCAL_PATTERNS]
        return jobs

    @staticmethod
    def _collect_ideas(seed: str, jobs: list[tuple[str, str, str]], suggestions: list[list[str]]
                       ) -> tuple[list[KeywordIdea], dict[str, list[dict[str, Any]]]]:
        """De-duplicate across all lists (first group wins), drop irrelevant ones, tag intent."""
        ideas: list[KeywordIdea] = []
        seen = {seed}
        region_words = frozenset(w for job in jobs if job[0] == "local" for w in job[1].split()
                                 if w not in {"in", "best", "top", "cost", "near", "me", "…"})
        groups_out: dict[str, list[dict[str, Any]]] = {}
        for (group, modifier, _), found in zip(jobs, suggestions):
            kept = []
            for position, raw in enumerate(found, start=1):
                kw = normalize(raw)
                if kw in seen or not is_relevant(kw, seed):
                    continue
                seen.add(kw)
                intent = classify_intent(kw)
                ideas.append(KeywordIdea(kw, group, modifier, position, intent, FUNNEL[intent],
                                         keyword_label(kw, group, region_words)))
                kept.append(kw)
            if kept:
                groups_out.setdefault(group, []).append({"modifier": modifier, "keywords": kept})
        return ideas, groups_out

    async def _suggest(self, http: HttpClient, serp: SerpClient, query: str, request: KeywordRequest,
                       stats: _Stats) -> list[str]:
        stats.sent += 1
        try:
            return await self._google_suggest(http, query, request)
        except (FetchError, ValueError) as exc:
            if request.source == "google" and serp.configured:
                try:
                    stats.serp_fallbacks += 1
                    return await serp.autocomplete(query, request.country, request.language)
                except Exception as serp_exc:
                    exc = serp_exc
            stats.failed += 1
            stats.errors.append(f"{query!r}: {exc}")
            return []

    async def _google_suggest(self, http: HttpClient, query: str, request: KeywordRequest) -> list[str]:
        # "chrome" returns up to 15 completions per query (same depth as AnswerSocrates); "firefox" returns 10.
        params = {"client": "chrome", "q": query, "hl": request.language, "gl": request.country,
                  "ie": "utf-8", "oe": "utf-8"}
        if request.source == "youtube":
            params["ds"] = "yt"
        response = await http.request("GET", GOOGLE_SUGGEST, params=params)
        data = json.loads(response.content.decode("utf-8", errors="replace"))
        if not (isinstance(data, list) and len(data) > 1 and isinstance(data[1], list)):
            raise ValueError("unexpected autocomplete response")
        return [s for s in data[1] if isinstance(s, str)]
