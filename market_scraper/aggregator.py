"""Runs every selected platform concurrently and assembles the consolidated JSON payload."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import Any

import httpx

from . import analysis
from .browser import PageFetcher
from .config import Settings
from .http import HttpClient
from .models import Record, SearchRequest
from .serp import SerpClient
from .sources import SOURCE_REGISTRY, Source, SourceContext
from .utils import utc_now_iso

log = logging.getLogger(__name__)

ALL_PLATFORMS: tuple[str, ...] = tuple(SOURCE_REGISTRY)


class Aggregator:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._transport = transport  # injectable for tests

    def availability(self) -> dict[str, tuple[bool, str]]:
        """Which platforms have credentials configured. Opens no connections."""
        http = HttpClient(self.settings, transport=self._transport)
        ctx = SourceContext(self.settings, http, SerpClient(self.settings, http), PageFetcher(self.settings, http))
        return {name: cls(ctx).availability() for name, cls in SOURCE_REGISTRY.items()}

    async def run(self, request: SearchRequest, platforms: Sequence[str] = ALL_PLATFORMS) -> dict[str, Any]:
        unknown = set(platforms) - set(SOURCE_REGISTRY)
        if unknown:
            raise ValueError(f"Unknown platform(s): {', '.join(sorted(unknown))}")

        started = time.perf_counter()
        status: dict[str, dict[str, Any]] = {}
        records: list[Record] = []

        async with HttpClient(self.settings, transport=self._transport) as http, \
                PageFetcher(self.settings, http) as pages:
            ctx = SourceContext(self.settings, http, SerpClient(self.settings, http), pages)
            runnable: list[Source] = []
            for name in platforms:
                source = SOURCE_REGISTRY[name](ctx)
                available, reason = source.availability()
                if available:
                    runnable.append(source)
                else:
                    status[name] = {"status": "skipped", "count": 0, "message": reason}

            for source, found, error, elapsed in await asyncio.gather(
                    *(self._run_source(s, request) for s in runnable)):
                records.extend(found)
                entry: dict[str, Any] = {"status": "error" if error else "ok", "count": len(found),
                                         "elapsed_seconds": round(elapsed, 2)}
                if error:
                    entry["message"] = error
                if source.warnings:
                    entry["warnings"] = source.warnings
                status[source.name] = entry

        records = self._dedupe(records)
        analysis.SentimentScorer().apply(records)
        order = {name: i for i, name in enumerate(SOURCE_REGISTRY)}
        records.sort(key=lambda r: (order.get(r.platform, 99), -r.engagement_score))

        return {
            "query": request.query,
            "generated_at": utc_now_iso(),
            "duration_seconds": round(time.perf_counter() - started, 2),
            "total_results": len(records),
            "platforms": {name: status[name] for name in platforms if name in status},
            "insights": {
                "trending_topics": analysis.trending_topics(records, request.query),
                "audience_questions": analysis.audience_questions(records),
                "open_queries": analysis.open_queries(records),
                "platform_breakdown": analysis.platform_breakdown(records),
                "pain_points": analysis.pain_points(records),
                "sentiment": analysis.sentiment_summary(records),
            },
            "results": [r.to_dict() for r in records],
        }

    async def _run_source(self, source: Source, request: SearchRequest
                          ) -> tuple[Source, list[Record], str | None, float]:
        started = time.perf_counter()
        try:
            found = await asyncio.wait_for(source.collect(request), timeout=self.settings.source_timeout)
            log.info("%s: %d records", source.name, len(found))
            return source, found, None, time.perf_counter() - started
        except asyncio.TimeoutError:
            message = f"timed out after {self.settings.source_timeout:.0f}s"
        except Exception as exc:  # one platform failing must never sink the whole run
            log.debug("%s failed", source.name, exc_info=True)
            message = f"{type(exc).__name__}: {exc}"
        log.error("%s: %s", source.name, message)
        return source, [], message, time.perf_counter() - started

    @staticmethod
    def _dedupe(records: list[Record]) -> list[Record]:
        unique: dict[tuple[str, str, str, str], Record] = {}
        for record in records:
            key = record.dedupe_key()
            if key not in unique or record.engagement_score > unique[key].engagement_score:
                unique[key] = record
        return list(unique.values())
