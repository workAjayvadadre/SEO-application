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
