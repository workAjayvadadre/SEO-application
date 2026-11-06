"""Instagram / Facebook / YouTube questions: question-focused discovery, YouTube fallback, question extraction."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from market_scraper import Aggregator, SearchRequest, Settings
from market_scraper.analysis import open_queries
from market_scraper.models import Record
from market_scraper.sources.youtube import YouTubeSource

BASE = Settings(max_retries=0, backoff_base=0.001, backoff_max=0.01, requests_per_second_per_host=0)


@pytest.fixture(autouse=True)
def reset_youtube_flag():
    YouTubeSource._api_down_until = 0.0
    yield
    YouTubeSource._api_down_until = 0.0


def serp_handler(queries: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.googleapis.com":
            raise httpx.ConnectTimeout("blocked by the corporate firewall")
        if request.url.host == "google.serper.dev":
            q = json.loads(request.content)["q"]
            queries.append(q)
            site = q.split()[0].removeprefix("site:")
            asks = " what OR how" in q
            link = {"youtube.com": "https://www.youtube.com/watch?v=abc123XYZ",
                    "instagram.com": "https://www.instagram.com/p/Q1/",
                    "facebook.com": "https://www.facebook.com/x/posts/1"}[site]
            title = {"youtube.com": "Is IVF painful? Doctor explains", "instagram.com": "IVF vs. egg freezing: what's the difference? Both are...",
                     "facebook.com": "Fertility clinic page"}[site]
            organic = [{"title": title, "link": link + ("q" if asks else ""), "position": 1,
                        "snippet": "Many ask: how long does IVF take? It varies." if site == "facebook.com" else ""}]
            return httpx.Response(200, json={"organic": organic})
        return httpx.Response(404)
    return handler


def test_blocked_youtube_api_falls_back_to_google_and_social_questions_surface():
    queries: list[str] = []
    settings = replace(BASE, serper_api_key="k", youtube_api_key="y")
    payload = asyncio.run(Aggregator(settings, transport=httpx.MockTransport(serp_handler(queries))).run(
        SearchRequest(query="ivf", limit=5, comments_per_item=0), ["youtube", "instagram", "facebook"]))

    yt = payload["platforms"]["youtube"]
    assert yt["status"] == "ok" and yt["count"] == 2, "videos found through Google instead of failing"
    assert any("unreachable" in w for w in yt.get("warnings", []))
    assert YouTubeSource._api_down_until > 0, "the block is remembered so the next search skips the wait"
    for site in ("youtube.com", "instagram.com", "facebook.com"):
        assert any(q.startswith(f"site:{site}") and "what OR how" in q for q in queries), f"question search on {site}"

    asked = {(q["platform"], q["question"]) for q in payload["insights"]["open_queries"]}
    assert ("youtube", "Is IVF painful? Doctor explains") in asked
    assert ("instagram", "Egg freezing: what's the difference?") in asked, "question taken from mid-title"
    assert ("facebook", "How long does IVF take?") in asked, "question taken from the caption"


def test_question_extraction_leaves_other_platforms_alone():
    recs = [Record("google", "https://g/1", "IVF cost explained. What affects it? Read more", "", 1.0, "organic_result"),
            Record("instagram", "https://i/1", "Clinic tour", "No questions here.", 1.0, "serp_discovery")]
    assert open_queries(recs) == []
