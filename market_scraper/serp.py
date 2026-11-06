"""Google results through a managed SERP API (Serper.dev or SerpApi).

Both providers handle proxies, CAPTCHAs and parsing on their side, so this is far more reliable
than scraping google.com directly. Responses are normalised into one `SerpResult` shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from .config import Settings
from .http import HttpClient

SERPER_URL = "https://google.serper.dev/search"
SERPAPI_URL = "https://serpapi.com/search.json"


@dataclass(slots=True)
class SerpResult:
    query: str
    organic: list[dict[str, Any]] = field(default_factory=list)  # title, link, snippet, position, date
    people_also_ask: list[dict[str, Any]] = field(default_factory=list)  # question, snippet, link
    related_searches: list[str] = field(default_factory=list)

    @property
    def search_url(self) -> str:
        return google_search_url(self.query)


def google_search_url(query: str) -> str:
    return "https://www.google.com/search?" + urlencode({"q": query})


def parse_serper(query: str, data: dict[str, Any]) -> SerpResult:
    return SerpResult(
        query=query,
        organic=[
            {"title": o.get("title", ""), "link": o.get("link", ""), "snippet": o.get("snippet", ""),
             "position": o.get("position") or i, "date": o.get("date")}
            for i, o in enumerate(data.get("organic") or [], start=1) if o.get("link")
        ],
        people_also_ask=[
            {"question": p.get("question", ""), "snippet": p.get("snippet", ""), "link": p.get("link")}
            for p in data.get("peopleAlsoAsk") or [] if p.get("question")
        ],
        related_searches=[r["query"] for r in data.get("relatedSearches") or [] if r.get("query")],
    )


def parse_serpapi(query: str, data: dict[str, Any]) -> SerpResult:
    return SerpResult(
        query=query,
        organic=[
            {"title": o.get("title", ""), "link": o.get("link", ""), "snippet": o.get("snippet", ""),
             "position": o.get("position") or i, "date": o.get("date")}
            for i, o in enumerate(data.get("organic_results") or [], start=1) if o.get("link")
        ],
        people_also_ask=[
            {"question": p.get("question", ""), "snippet": p.get("snippet", ""), "link": p.get("link")}
            for p in data.get("related_questions") or [] if p.get("question")
        ],
        related_searches=[r["query"] for r in data.get("related_searches") or [] if r.get("query")],
    )


TIME_TBS = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}


class SerpClient:
    def __init__(self, settings: Settings, http: HttpClient) -> None:
        self.settings = settings
        self.http = http
        self.provider = settings.serp_provider

    @property
    def configured(self) -> bool:
        return (self.provider == "serper" and bool(self.settings.serper_api_key)) or (
            self.provider == "serpapi" and bool(self.settings.serpapi_api_key))

    async def search(self, query: str, num: int = 10, location: str | None = None,
                     time_range: str | None = None) -> SerpResult:
        """`location` localises Google results (e.g. "Kolkata,West Bengal,India");
        `time_range` (day / week / month / year) keeps only pages Google dates in that window."""
        num = max(1, min(num, 100))
        tbs = TIME_TBS.get(time_range or "")
        if self.provider == "serper" and self.settings.serper_api_key:
            body = {"q": query, "gl": self.settings.search_country, "hl": self.settings.search_language, "num": num}
            if location:
                body["location"] = location
            if tbs:
                body["tbs"] = tbs
            data = await self.http.json(
                "POST", SERPER_URL, use_proxy=False,
                headers={"X-API-KEY": self.settings.serper_api_key, "Content-Type": "application/json"},
                json=body,
            )
            return parse_serper(query, data)
        if self.provider == "serpapi" and self.settings.serpapi_api_key:
            params = {"engine": "google", "q": query, "api_key": self.settings.serpapi_api_key,
                      "gl": self.settings.search_country, "hl": self.settings.search_language, "num": num}
            if location:
                params["location"] = location
            if tbs:
                params["tbs"] = tbs
            data = await self.http.get_json(SERPAPI_URL, use_proxy=False, params=params)
            return parse_serpapi(query, data)
        raise RuntimeError("No SERP provider configured (set SERPER_API_KEY or SERPAPI_API_KEY)")

    async def autocomplete(self, query: str, country: str, language: str) -> list[str]:
        """Google Autocomplete through the SERP provider (paid fallback for the free endpoint)."""
        if self.provider == "serper" and self.settings.serper_api_key:
            data = await self.http.json(
                "POST", SERPER_URL.replace("/search", "/autocomplete"), use_proxy=False,
                headers={"X-API-KEY": self.settings.serper_api_key, "Content-Type": "application/json"},
                json={"q": query, "gl": country, "hl": language},
            )
        elif self.provider == "serpapi" and self.settings.serpapi_api_key:
            data = await self.http.get_json(
                SERPAPI_URL, use_proxy=False,
                params={"engine": "google_autocomplete", "q": query, "gl": country, "hl": language,
                        "api_key": self.settings.serpapi_api_key},
            )
        else:
            raise RuntimeError("No SERP provider configured")
        return [s["value"] for s in data.get("suggestions") or [] if isinstance(s, dict) and s.get("value")]
