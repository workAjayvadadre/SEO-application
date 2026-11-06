"""Post-processing: sentiment, trending topics, audience questions and pain points."""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from .models import USER_GENERATED_TYPES, Record
from .utils import query_terms

log = logging.getLogger(__name__)

STOPWORDS = frozenset("""
a about above after again against all also am an and any are aren't as at be because been before being
below between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down
during each even ever every few for from further get gets getting got had hadn't has hasn't have haven't
having he her here hers herself him himself his how i if in into is isn't it it's its itself just know
let's like make many may me might more most much must my myself need new no nor not now of off on once
one only or other our ours ourselves out over own really said same say says see she should shouldn't so
some still such than that that's the their theirs them themselves then there there's these they this
those through to too under until up upon us use used using very via want was wasn't way we well were
weren't what what's when where which while who whom why will with won't would wouldn't yes yet you your
yours yourself yourselves i'm i've i'd i'll you're you've they're we're thing things lot lots go going
anyone someone something anything people time year years day days back good great best better first
http https www com org net html amp quot reddit quora youtube instagram facebook google video videos
comment comments post posts answer answers question questions link deleted removed edit update
""".split())

_TOKEN = re.compile(r"[a-z][a-z0-9'+\-]*[a-z0-9+]|[a-z]")


class SentimentScorer:
    """VADER (tuned for social-media text). Degrades to no-op if the package is missing."""

    def __init__(self) -> None:
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            self._analyzer: Any = SentimentIntensityAnalyzer()
        except ImportError:
            log.warning("vaderSentiment not installed; sentiment fields will be null")
            self._analyzer = None

    def apply(self, records: Iterable[Record]) -> None:
        if self._analyzer is None:
            return
        for record in records:
            if record.record_type not in USER_GENERATED_TYPES:
                continue
            text = f"{record.title}. {record.content_snippet}" if record.record_type != "comment" else record.content_snippet
            compound = self._analyzer.polarity_scores(text)["compound"]
            record.sentiment = round(compound, 4)
            record.sentiment_label = "positive" if compound >= 0.05 else "negative" if compound <= -0.05 else "neutral"


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _is_term(token: str) -> bool:
    return len(token) > 2 and token not in STOPWORDS and not token.replace("-", "").isdigit()


def trending_topics(records: list[Record], query: str, top_n: int = 25, min_mentions: int = 2) -> list[dict[str, Any]]:
    """Rank unigrams/bigrams by engagement-weighted document frequency.

    Terms seen on several platforms get a boost: cross-platform chatter is a stronger trend signal
    than one viral thread. Words from the query itself are excluded since they're in everything.
    """
    query_set = set(query_terms(query))
    scores: Counter[str] = Counter()
    mentions: Counter[str] = Counter()
    platforms: dict[str, set[str]] = defaultdict(set)

    for record in records:
        tokens = _tokens(f"{record.title} {record.content_snippet}")
        terms = {t for t in tokens if _is_term(t) and t not in query_set}
        terms |= {
            f"{a} {b}" for a, b in zip(tokens, tokens[1:])
            if a != b and _is_term(a) and _is_term(b) and not (a in query_set and b in query_set)
        }
        weight = 1.0 + math.log1p(max(record.engagement_score, 0.0))
        for term in terms:
            scores[term] += weight * (1.5 if " " in term else 1.0)
            mentions[term] += 1
            platforms[term].add(record.platform)

    ranked = [
        {"term": term, "score": round(score * (1 + 0.25 * (len(platforms[term]) - 1)), 2),
         "mentions": mentions[term], "platforms": sorted(platforms[term])}
        for term, score in scores.items() if mentions[term] >= min_mentions
    ]
    ranked.sort(key=lambda t: t["score"], reverse=True)
    return ranked[:top_n]


def audience_questions(records: list[Record], top_n: int = 30) -> list[dict[str, Any]]:
    """Questions people actually ask: Google PAA, Quora questions and question-style post titles."""
    seen: set[str] = set()
    questions = []
    for r in sorted(records, key=lambda r: r.engagement_score, reverse=True):
        is_question = r.record_type in {"people_also_ask", "question"} or (
            r.record_type in {"post", "video"} and r.title.rstrip().endswith("?"))
        key = r.title.casefold().strip()
        if is_question and key and key not in seen:
            seen.add(key)
            questions.append({"question": r.title, "platform": r.platform, "source_url": r.source_url,
                              "engagement_score": r.engagement_score})
    return questions[:top_n]


_QUESTION_START = re.compile(r"^(what|why|how|when|where|which|who|is|are|can|could|does|do|did|should|will|"
                             r"would|has|have|kya|kaise|kab|kyun)\b", re.I)


def _is_question(text: str) -> bool:
    text = (text or "").strip()
    return text.endswith("?") or bool(_QUESTION_START.match(text))


def _answers(r: Record) -> int | None:
    """How many replies the question already has, when the platform tells us."""
    m = r.metrics or {}
    for key in ("question_answer_count", "num_comments", "comments", "replies"):
        if isinstance(m.get(key), int):
            return m[key]
    return None


def _why_open(r: Record, answers: int | None) -> str:
    if r.record_type == "comment":
        return "asked in comments, usually unanswered by the creator"
    if r.record_type == "people_also_ask":
        return "Google surfaces it as a common question"
    if answers is not None:
        return f"{answers} answers so far"
    return "few visible answers"


def open_queries(records: list[Record], top_n: int = 40) -> list[dict[str, Any]]:
    """Questions where a clear, compliant answer would be welcome: asked with interest, answered thinly.

    Score = engagement / (1 + existing answers). Unknown answer counts (search listings) score as 1 answer.
    Each item says why it is open and what kind of content could respond (NMC Class A education).
    """
    seen: set[str] = set()
    out = []
    for r in records:
        text = (r.content_snippet if r.record_type == "comment" else r.title) or ""
        if not (r.record_type in {"people_also_ask", "question"} or _is_question(text)):
            continue
        key = re.sub(r"\W+", " ", text.casefold()).strip()[:140]
        if not key or key in seen:
            continue
        seen.add(key)
        answers = _answers(r)
        score = (r.engagement_score or 0.5) / (1 + (answers if answers is not None else 1))
        out.append({"question": text.strip()[:300], "platform": r.platform, "record_type": r.record_type,
                    "existing_answers": answers, "engagement_score": r.engagement_score,
                    "opportunity": round(score, 3), "why_open": _why_open(r, answers), "source_url": r.source_url,
                    "published_at": r.published_at, "suggested_class": "A (public-health education)"})
    out.sort(key=lambda q: q["opportunity"], reverse=True)
    return out[:top_n]


def platform_breakdown(records: list[Record]) -> dict[str, Any]:
    """Per platform: volume, types, average engagement, newest item and top themes."""
    by: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        by[r.platform].append(r)
    out = {}
    for platform, items in by.items():
        dates = sorted((r.published_at for r in items if r.published_at), reverse=True)
        out[platform] = {
            "items": len(items),
            "types": dict(Counter(r.record_type for r in items)),
            "avg_engagement": round(sum(r.engagement_score for r in items) / len(items), 3),
            "newest": dates[0] if dates else None,
            "top_themes": [t["term"] for t in trending_topics(items, "", top_n=8, min_mentions=2)],
            "questions": sum(1 for r in items if _is_question(r.title)),
        }
    return out


def pain_points(records: list[Record], top_n: int = 20) -> list[dict[str, Any]]:
    """Most-engaged negative user content: complaints, frustrations, unmet needs."""
    negatives = [r for r in records if r.sentiment is not None and r.sentiment <= -0.2 and r.content_snippet]
    negatives.sort(key=lambda r: (r.engagement_score, -r.sentiment), reverse=True)
    return [
        {"platform": r.platform, "record_type": r.record_type, "title": r.title,
         "content_snippet": r.content_snippet, "sentiment": r.sentiment,
         "engagement_score": r.engagement_score, "source_url": r.source_url}
        for r in negatives[:top_n]
    ]


def sentiment_summary(records: list[Record]) -> dict[str, Any]:
    def summarize(items: list[Record]) -> dict[str, Any]:
        scored = [r for r in items if r.sentiment is not None]
        labels = Counter(r.sentiment_label for r in scored)
        return {
            "scored_items": len(scored),
            "average": round(sum(r.sentiment for r in scored) / len(scored), 4) if scored else None,  # type: ignore[misc]
            "positive": labels["positive"], "neutral": labels["neutral"], "negative": labels["negative"],
        }

    by_platform: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        by_platform[r.platform].append(r)
    return {"overall": summarize(records),
            "by_platform": {p: summarize(items) for p, items in by_platform.items()}}
