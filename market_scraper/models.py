"""Unified data model shared by every platform module."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .utils import utc_now_iso


class Platform:
    GOOGLE = "google"
    REDDIT = "reddit"
    QUORA = "quora"
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"


# Record types that carry real user voice (scored for sentiment). SERP listings are excluded.
USER_GENERATED_TYPES = frozenset(
    {"post", "comment", "question", "answer", "video", "hashtag_post", "profile_post", "page_post"}
)


@dataclass(slots=True)
class Record:
    """One normalised item. The first six fields are the contract; the rest is enrichment."""

    platform: str
    source_url: str
    title: str
    content_snippet: str
    engagement_score: float
    record_type: str
    timestamp: str = field(default_factory=utc_now_iso)  # when we collected it
    published_at: str | None = None  # when the platform says it was created
    author: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)  # raw platform counters
    sentiment: float | None = None  # VADER compound, -1..1
    sentiment_label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {"timestamp": data.pop("timestamp"), **data}

    def dedupe_key(self) -> tuple[str, str, str, str]:
        return (self.platform, self.record_type, self.source_url, self.title.casefold()[:120])


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str
    limit: int = 10
    comments_per_item: int = 10
    hashtags: tuple[str, ...] = ()
    # Google "location" for local results, e.g. "Kolkata,West Bengal,India" (None = whole country).
    geo_location: str | None = None
    # Only content from the last day / week / month / year (None = any time).
    time_range: str | None = None
