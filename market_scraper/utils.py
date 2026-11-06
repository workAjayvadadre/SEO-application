"""Small, dependency-free helpers shared by every module."""

from __future__ import annotations

import html
import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

_WHITESPACE = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9]+")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def epoch_to_iso(ts: float | int | str | None) -> str | None:
    if ts in (None, ""):
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")


def clean_text(text: Any, max_len: int = 600) -> str:
    """Unescape HTML entities, collapse whitespace and truncate on a word boundary."""
    if not text:
        return ""
    value = _WHITESPACE.sub(" ", html.unescape(str(text))).strip()
    if len(value) <= max_len:
        return value
    cut = value[: max_len - 1]
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut) + "…"


def engagement_score(metrics: Mapping[str, Any], weights: Mapping[str, float]) -> float:
    """Weighted sum of log-scaled counters so a 1M-view video doesn't drown out everything else."""
    total = 0.0
    for key, weight in weights.items():
        try:
            value = max(float(metrics.get(key) or 0), 0.0)
        except (TypeError, ValueError):
            value = 0.0
        total += weight * math.log1p(value)
    return round(total, 3)


def rank_score(position: int | None) -> float:
    """Engagement proxy for search results, where only the rank is known (#1 -> 10.0, #4 -> 5.0)."""
    if not position or position < 1:
        return 0.0
    return round(10.0 / math.sqrt(position), 3)


def query_terms(query: str) -> list[str]:
    return [t for t in _WORD.findall(query.lower()) if len(t) > 2]


def mentions_query(text: str | None, query: str) -> bool:
    """True when the text contains at least one meaningful query term."""
    if not text:
        return False
    lowered = text.lower()
    terms = query_terms(query) or [query.lower().strip()]
    return any(term in lowered for term in terms)
