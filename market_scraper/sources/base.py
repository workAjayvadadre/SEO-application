"""Contract every platform module implements."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from typing import ClassVar

from ..browser import PageFetcher
from ..config import Settings
from ..http import HttpClient
from ..models import Record, SearchRequest
from ..serp import SerpClient
from ..utils import clean_text, rank_score


@dataclass(slots=True)
class SourceContext:
    """Shared, already-open resources handed to every source."""

    settings: Settings
    http: HttpClient
    serp: SerpClient
    pages: PageFetcher


class Source(ABC):
    name: ClassVar[str]

    def __init__(self, ctx: SourceContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.http = ctx.http
        self.serp = ctx.serp
        self.warnings: list[str] = []  # partial failures that didn't sink the whole source
        self.log = logging.getLogger(f"market_scraper.sources.{self.name}")

    @abstractmethod
    def availability(self) -> tuple[bool, str]:
        """(True, '') if credentials are present, else (False, human-readable reason)."""

    @abstractmethod
    async def collect(self, request: SearchRequest) -> list[Record]:
        """Fetch and normalise records for the query."""

    async def settle(self, tasks: Iterable[Awaitable[list[Record]]], label: str, *,
                     enrichment: bool = False) -> list[Record]:
        """Run sub-tasks concurrently and keep whatever succeeded.

        Individual failures become warnings. If *every* sub-task fails, the first error is raised,
        unless `enrichment=True`. Enrichment (comments, answers) is extra detail on records we already
        have, so losing all of it must never discard those primary records.
        """
        results = await asyncio.gather(*tasks, return_exceptions=True)
        records: list[Record] = []
        errors: list[BaseException] = []
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                errors.append(result)
                self.warnings.append(f"{label}: {result}")
                self.log.warning("%s failed: %s", label, result)
            else:
                records.extend(result)
        if errors and len(errors) == len(results) and not enrichment:
            raise errors[0]
        return records

    async def site_discovery(self, site: str, request: SearchRequest, *, link_filter: str | None = None) -> list[Record]:
        """Public posts on `site` found through the SERP provider: a plain query plus a question-focused one,
        so the questions people ask there are not crowded out by brand pages. Deduplicated by link."""
        queries = [f"site:{site} {request.query}",
                   f'site:{site} {request.query} what OR how OR why OR when OR "can i" OR "should i" OR "is it"']
        results = await asyncio.gather(*(self.serp.search(q, num=request.limit, time_range=request.time_range)
                                         for q in queries), return_exceptions=True)
        if all(isinstance(r, BaseException) for r in results):
            raise results[0]  # type: ignore[misc]
        seen: set[str] = set()
        records: list[Record] = []
        for result in results:
            if isinstance(result, BaseException):
                self.warnings.append(f"{site} question search: {result}")
                continue
            for o in result.organic:
                link = o["link"]
                if (link_filter or site) not in link or link in seen:
                    continue
                seen.add(link)
                records.append(Record(platform=self.name, record_type="serp_discovery", source_url=link,
                                      title=clean_text(o["title"], 300), content_snippet=clean_text(o["snippet"]),
                                      engagement_score=rank_score(o["position"]), published_at=o.get("date"),
                                      metrics={"rank": o["position"]}))
        return records[: request.limit * 2]
