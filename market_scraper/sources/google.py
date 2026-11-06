"""Google: organic results, People Also Ask questions and related searches (via SERP API)."""

from __future__ import annotations

from ..models import Platform, Record, SearchRequest
from ..serp import google_search_url
from ..utils import clean_text, rank_score
from .base import Source


class GoogleSource(Source):
    name = Platform.GOOGLE

    def availability(self) -> tuple[bool, str]:
        return (True, "") if self.serp.configured else (False, "Set SERPER_API_KEY or SERPAPI_API_KEY")

    async def collect(self, request: SearchRequest) -> list[Record]:
        try:
            result = await self.serp.search(request.query, num=request.limit, location=request.geo_location,
                                            time_range=request.time_range)
        except Exception as exc:  # unknown location name: fall back to country-wide results
            if not request.geo_location:
                raise
            self.warnings.append(f"google: location {request.geo_location!r} not accepted ({exc}); used country-wide results")
            result = await self.serp.search(request.query, num=request.limit, time_range=request.time_range)
        records: list[Record] = []

        for item in result.organic[: request.limit]:
            records.append(Record(
                platform=self.name, record_type="organic_result",
                source_url=item["link"], title=clean_text(item["title"], 300),
                content_snippet=clean_text(item["snippet"]),
                engagement_score=rank_score(item["position"]),
                published_at=item.get("date"), metrics={"rank": item["position"]},
            ))

        for rank, item in enumerate(result.people_also_ask, start=1):
            records.append(Record(
                platform=self.name, record_type="people_also_ask",
                source_url=item.get("link") or result.search_url, title=clean_text(item["question"], 300),
                content_snippet=clean_text(item.get("snippet")),
                engagement_score=rank_score(rank), metrics={"rank": rank},
            ))

        for rank, related in enumerate(result.related_searches, start=1):
            records.append(Record(
                platform=self.name, record_type="related_search",
                source_url=google_search_url(related), title=clean_text(related, 300), content_snippet="",
                engagement_score=rank_score(rank), metrics={"rank": rank},
            ))
        return records
