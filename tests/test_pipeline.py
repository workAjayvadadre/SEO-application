"""Offline tests: every network call is served by an httpx.MockTransport."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from market_scraper import Aggregator, SearchRequest, Settings
from market_scraper.http import API_USER_AGENT, FetchError, HttpClient, ProxyPool
from market_scraper.serp import parse_serpapi, parse_serper
from market_scraper.sources.quora import parse_qapage

BASE = Settings(max_retries=3, backoff_base=0.001, backoff_max=0.01, requests_per_second_per_host=0)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- http layer

def test_retries_transient_errors_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        if calls["n"] == 2:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    async def go():
        async with HttpClient(BASE, transport=httpx.MockTransport(handler)) as http:
            return await http.get_json("https://api.example.com/x")

    assert run(go()) == {"ok": True}
    assert calls["n"] == 3


def test_non_retryable_status_fails_fast():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="nope")

    async def go():
        async with HttpClient(BASE, transport=httpx.MockTransport(handler)) as http:
            await http.get_json("https://api.example.com/x")

    with pytest.raises(FetchError) as info:
        run(go())
    assert info.value.status == 404 and calls["n"] == 1


def test_403_retried_only_for_browser_pages_with_proxies():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403)

    async def go(settings: Settings, browser_like: bool) -> int:
        calls["n"] = 0
        async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(FetchError):
                await http.request("GET", "https://site.example.com/", browser_like=browser_like)
        return calls["n"]

    proxied = replace(BASE, proxies=("http://p1:8080", "http://p2:8080"))
    assert run(go(proxied, False)) == 1                      # API call: never retry a 403
    assert run(go(BASE, True)) == 1                          # same IP would just be blocked again
    assert run(go(proxied, True)) == BASE.max_retries + 1    # new proxy each attempt: worth retrying


def test_failed_enrichment_keeps_primary_records():
    def blocked_quora(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.quora.com":
            return httpx.Response(403)
        return fake_platforms(request)

    settings = replace(BASE, serper_api_key="k")
    payload = run(Aggregator(settings, transport=httpx.MockTransport(blocked_quora)).run(
        SearchRequest("standing desk", limit=5, comments_per_item=5), ["quora"]))
    quora = payload["platforms"]["quora"]
    assert quora["status"] == "ok"
    assert quora["count"] == 1  # the question survives even though its answers were blocked
    assert quora["warnings"]


def test_rotating_user_agent_header_is_sent():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200, text="<html></html>")

    async def go():
        async with HttpClient(BASE, transport=httpx.MockTransport(handler)) as http:
            for _ in range(5):
                await http.get_text("https://site.example.com/")

    run(go())
    assert all(ua.startswith("Mozilla/5.0") for ua in seen)


def test_official_api_calls_use_client_user_agent():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200, json={})

    async def go():
        async with HttpClient(BASE, transport=httpx.MockTransport(handler)) as http:
            await http.get_json("https://api.example.com/v1", use_proxy=False)
            await http.get_json("https://api.example.com/v1", use_proxy=False, headers={"User-Agent": "custom/1"})

    run(go())
    assert seen[0] == API_USER_AGENT  # browser UAs trip corporate web filters (Zscaler) on API hosts
    assert seen[1] == "custom/1"  # explicit headers (e.g. Reddit's required UA) still win


def test_proxy_pool_skips_benched_proxies():
    pool = ProxyPool(["http://a:1", "http://b:2"])
    pool.penalize("http://a:1")
    assert {pool.next() for _ in range(4)} == {"http://b:2"}


# ---------------------------------------------------------------- parsers

def test_serp_parsers_normalise_both_providers():
    serper = parse_serper("q", {
        "organic": [{"title": "T", "link": "https://a.com", "snippet": "S", "position": 1}],
        "peopleAlsoAsk": [{"question": "Why?", "snippet": "Because", "link": "https://b.com"}],
        "relatedSearches": [{"query": "q cheap"}],
    })
    serpapi = parse_serpapi("q", {
        "organic_results": [{"title": "T", "link": "https://a.com", "snippet": "S", "position": 1}],
        "related_questions": [{"question": "Why?", "snippet": "Because", "link": "https://b.com"}],
        "related_searches": [{"query": "q cheap"}],
    })
    for result in (serper, serpapi):
        assert result.organic[0]["link"] == "https://a.com"
        assert result.people_also_ask[0]["question"] == "Why?"
        assert result.related_searches == ["q cheap"]


def test_parse_quora_qapage_jsonld():
    doc = {"@context": "https://schema.org", "@type": "QAPage", "mainEntity": {
        "@type": "Question", "name": "Is X worth it?", "answerCount": 2,
        "suggestedAnswer": [
            {"@type": "Answer", "text": "Not really, too expensive.", "upvoteCount": 12,
             "author": {"@type": "Person", "name": "A"}},
            {"@type": "Answer", "text": "Yes!", "upvoteCount": 3},
        ]}}
    html = f'<html><script type="application/ld+json">{json.dumps(doc)}</script></html>'
    qa = parse_qapage(html)
    assert qa["question"] == "Is X worth it?"
    assert [a["upvotes"] for a in qa["answers"]] == [12, 3]
    assert qa["answers"][0]["author"] == "A"


# ---------------------------------------------------------------- end to end

def fake_platforms(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "google.serper.dev":
        q = json.loads(request.content)["q"]
        if q.startswith("site:quora.com"):
            return httpx.Response(200, json={"organic": [
                {"title": "Why are standing desks so expensive? - Quora",
                 "link": "https://www.quora.com/Why-are-standing-desks-so-expensive", "snippet": "...", "position": 1}]})
        if q.startswith("site:"):
            return httpx.Response(200, json={"organic": []})
        return httpx.Response(200, json={
            "organic": [{"title": "Best standing desk 2026", "link": "https://a.com/best", "snippet": "Motor noise and wobble compared", "position": 1}],
            "peopleAlsoAsk": [{"question": "Are standing desks worth it?", "snippet": "Back pain relief", "link": "https://b.com"}],
            "relatedSearches": [{"query": "standing desk wobble fix"}]})
    if host == "www.quora.com":
        doc = {"@type": "QAPage", "mainEntity": {"@type": "Question", "name": "Why are standing desks so expensive?",
               "suggestedAnswer": [{"text": "The motor wobble is terrible and support is awful.", "upvoteCount": 40}]}}
        return httpx.Response(200, text=f'<script type="application/ld+json">{json.dumps(doc)}</script>')
    if host == "www.reddit.com" and path == "/api/v1/access_token":
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    if host == "oauth.reddit.com" and path == "/search":
        assert request.headers["Authorization"] == "bearer tok"
        return httpx.Response(200, json={"data": {"children": [{"kind": "t3", "data": {
            "id": "abc", "title": "My standing desk wobble is driving me crazy?", "selftext": "Terrible wobble at max height",
            "permalink": "/r/desks/comments/abc/x/", "score": 250, "num_comments": 40, "subreddit": "desks",
            "author": "u1", "created_utc": 1700000000}}]}})
    if host == "oauth.reddit.com" and path == "/comments/abc":
        return httpx.Response(200, json=[{}, {"data": {"children": [
            {"kind": "t1", "data": {"body": "Same, motor wobble is awful and support ignored me", "score": 90,
                                    "permalink": "/r/desks/comments/abc/x/c1/", "subreddit": "desks"}},
            {"kind": "t1", "data": {"body": "[deleted]", "score": 5}}]}}])
    if host == "www.googleapis.com" and path.endswith("/search"):
        return httpx.Response(200, json={"items": [{"id": {"videoId": "v1"}}]})
    if host == "www.googleapis.com" and path.endswith("/videos"):
        return httpx.Response(200, json={"items": [{"id": "v1", "snippet": {"title": "Standing desk review", "description": "Wobble test", "channelTitle": "C"},
                                                    "statistics": {"viewCount": "10000", "likeCount": "500", "commentCount": "30"}}]})
    if host == "www.googleapis.com" and path.endswith("/commentThreads"):
        return httpx.Response(403, json={"error": {"errors": [{"reason": "commentsDisabled"}]}})
    return httpx.Response(404)


def test_end_to_end_payload_shape():
    settings = replace(BASE, serper_api_key="k", reddit_client_id="id", reddit_client_secret="s",
                       youtube_api_key="y")
    payload = run(Aggregator(settings, transport=httpx.MockTransport(fake_platforms)).run(
        SearchRequest("standing desk", limit=5, comments_per_item=5)))

    status = payload["platforms"]
    assert status["google"]["status"] == status["reddit"]["status"] == status["youtube"]["status"] == "ok"
    assert status["quora"]["count"] == 2  # question + 1 answer
    assert status["facebook"]["status"] == "ok" and status["facebook"]["count"] == 0  # SERP discovery, no hits

    required = ["timestamp", "platform", "source_url", "title", "content_snippet", "engagement_score"]
    for item in payload["results"]:
        assert list(item)[:6] == required
    types = {(r["platform"], r["record_type"]) for r in payload["results"]}
    assert {("google", "people_also_ask"), ("reddit", "comment"), ("quora", "answer"), ("youtube", "video")} <= types
    assert not any(r["content_snippet"] == "[deleted]" for r in payload["results"])

    insights = payload["insights"]
    assert "wobble" in {t["term"] for t in insights["trending_topics"]}
    assert any(q["question"] == "Are standing desks worth it?" for q in insights["audience_questions"])
    assert insights["pain_points"], "negative, engaged comments should surface as pain points"


def test_missing_credentials_are_skipped_not_fatal():
    payload = run(Aggregator(BASE, transport=httpx.MockTransport(fake_platforms)).run(SearchRequest("x")))
    assert all(p["status"] == "skipped" for p in payload["platforms"].values())
    assert payload["results"] == []


def test_reddit_falls_back_to_serp_without_api_keys():
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "google.serper.dev" and json.loads(request.content)["q"].startswith("site:reddit.com"):
            return httpx.Response(200, json={"organic": [
                {"title": "Desk wobble at max height : r/desks", "position": 1, "snippet": "wobbles a lot",
                 "link": "https://www.reddit.com/r/desks/comments/abc/desk_wobble/"},
                {"title": "Blocked thread - Reddit", "position": 2, "snippet": "s",
                 "link": "https://www.reddit.com/r/office/comments/xyz/blocked/"},
                {"title": "r/desks", "position": 3, "snippet": "s", "link": "https://www.reddit.com/r/desks/"}]})
        if host == "www.reddit.com" and path == "/comments/abc.json":
            return httpx.Response(200, json=[
                {"data": {"children": [{"kind": "t3", "data": {
                    "id": "abc", "title": "Desk wobble at max height", "selftext": "It wobbles",
                    "permalink": "/r/desks/comments/abc/desk_wobble/", "score": 120, "num_comments": 9,
                    "subreddit": "desks"}}]}},
                {"data": {"children": [{"kind": "t1", "data": {
                    "body": "Mine too, terrible", "score": 30, "subreddit": "desks",
                    "permalink": "/r/desks/comments/abc/desk_wobble/c1/"}}]}}])
        if host == "www.reddit.com" and path == "/comments/xyz.json":
            return httpx.Response(403)
        return fake_platforms(request)

    settings = replace(BASE, serper_api_key="k")
    payload = run(Aggregator(settings, transport=httpx.MockTransport(handler)).run(
        SearchRequest("desk wobble", limit=5, comments_per_item=3), platforms=["reddit"]))
    reddit = [r for r in payload["results"] if r["platform"] == "reddit"]
    by_type = {(r["record_type"], r["title"]) for r in reddit}
    assert ("post", "Desk wobble at max height") in by_type      # enriched with real score
    assert ("post", "Blocked thread") in by_type                 # SERP-only record survives the 403
    assert any(r["record_type"] == "comment" for r in reddit)
    assert len([r for r in reddit if r["record_type"] == "post"]) == 2  # subreddit page filtered out
    assert payload["platforms"]["reddit"]["status"] == "ok"


def test_manual_answers_are_saved_and_attached_to_later_searches(tmp_path):
    from fastapi.testclient import TestClient
    from server import create_app

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "google.serper.dev":
            return httpx.Response(200, json={"organic": [{
                "title": "Can IVF fail? - Quora", "link": "https://www.quora.com/Can-IVF-fail",
                "snippet": "", "position": 1}]})
        return httpx.Response(403, text="blocked")  # the Quora page itself is unreadable

    store = tmp_path / "manual.json"
    app = create_app(replace(BASE, serper_api_key="k"), transport=httpx.MockTransport(handler),
                     allowed_hosts=["testserver"], manual_answers_file=store)
    with TestClient(app) as client:
        body = {"query": "ivf", "platforms": ["quora"], "limit": 3, "comments": 0}
        first = client.post("/api/search", json=body).json()["insights"]["open_queries"][0]
        assert first["answers"] == [] and first["manual_answers"] == []

        saved = client.put("/api/manual-answers", json={
            "source_url": first["source_url"], "question": first["question"], "answers": ["Yes, sometimes."]})
        assert saved.status_code == 200 and saved.json()["key"] == first["answer_key"]
        assert client.put("/api/manual-answers", json={
            "source_url": "javascript:alert(1)", "question": "x", "answers": ["y"]}).status_code == 422

        again = client.post("/api/search", json=body).json()["insights"]["open_queries"][0]
        assert [a["text"] for a in again["manual_answers"]] == ["Yes, sometimes."]
        listed = client.get("/api/manual-answers").json()["answers"]
        assert [a["text"] for a in listed[first["answer_key"]]] == ["Yes, sometimes."]

        client.put("/api/manual-answers", json={"source_url": first["source_url"], "question": first["question"],
                                                "answers": []})
        assert client.get("/api/manual-answers").json()["answers"] == {}
    assert store.exists(), "pasted answers persist on disk between server restarts"


def test_scan_location_localises_google_and_reaches_every_query():
    from fastapi.testclient import TestClient
    from server import create_app

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "google.serper.dev":
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"organic": []})
        return fake_platforms(request)

    app = create_app(replace(BASE, serper_api_key="k"), transport=httpx.MockTransport(handler),
                     allowed_hosts=["testserver"])
    with TestClient(app) as client:
        r = client.post("/api/search", json={"query": "ivf", "platforms": ["google", "quora"],
                                              "location": "Salt Lake Kolkata", "time_range": "week",
                                              "limit": 3, "comments": 0})
    assert r.status_code == 200, r.text
    google = next(b for b in seen if not b["q"].startswith("site:"))
    assert google["q"] == "ivf Salt Lake Kolkata" and google["location"] == "Kolkata,West Bengal,India"
    assert all(b.get("tbs") == "qdr:w" for b in seen), "time range reaches every SERP-backed platform"
    assert any(b["q"] == "site:quora.com ivf Salt Lake Kolkata" and "location" not in b for b in seen)
