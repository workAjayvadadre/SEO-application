"""Keyword engine, content planner and server endpoints, with Google Suggest mocked."""

from __future__ import annotations

import asyncio
import json
from io import BytesIO

import httpx
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from market_scraper import Settings
from market_scraper.keywords import KeywordEngine, KeywordRequest, classify_intent, cluster_keywords, is_relevant
from market_scraper.planner import _DAY_OFFSET, build_calendar, month_slots, suggest_title
from server import create_app

SETTINGS = Settings(max_retries=1, backoff_base=0.001, backoff_max=0.01, keyword_rps=0)

SUGGESTIONS = {
    "why": ["why ivf fails", "why ivf is expensive", "why ivf fails"],
    "how": ["how ivf works", "how ivf process works step by step"],
    "vs": ["ivf vs iui", "ivf vs icsi", "ivf vs iui success rate"],
    "cost": ["ivf cost in jaipur", "ivf cost in jaipur indira ivf"],
    "near": ["ivf near me", "ivf centre near me"],
    "c": ["ivf cost", "ivf cost in india", "ivf centre"],
    "s": ["ivf success rate", "ivf success rate by age", "ivf symptoms after transfer"],
    "t": ["ivf treatment", "ivf treatment cost", "ivf tips for success"],
}


def suggest(request: httpx.Request) -> httpx.Response:
    q = request.url.params.get("q", "")
    if request.url.host != "suggestqueries.google.com":
        return httpx.Response(404)
    if q == "ivf p":
        return httpx.Response(500)  # one broken call must not sink the run
    words = q.split()
    hits = next((v for k, v in SUGGESTIONS.items() if k in words), [f"{q} guide", "unrelated thing"])
    if request.url.params.get("ds") == "yt":
        hits = [f"{q} video", "ivf journey vlog", "ivf injections at home"]
    return httpx.Response(200, content=json.dumps([q, hits]).encode())


def run_engine(**kw):
    return asyncio.run(KeywordEngine(SETTINGS, transport=httpx.MockTransport(suggest)).run(KeywordRequest("ivf", **kw)))


def test_engine_groups_dedupes_and_filters():
    result = run_engine(region="Jaipur")
    words = [k["keyword"] for k in result["keywords"]]
    assert len(words) == len(set(words)), "keywords must be unique across groups"
    assert "unrelated thing" not in words, "suggestions without the seed are dropped"
    assert "ivf cost in jaipur" in words
    assert result["counts"]["local"] > 0
    assert result["requests"]["failed"] == 1 and result["total_keywords"] > 20
    assert any(m["modifier"] == "vs" for m in result["groups"]["comparisons"])


def test_intent_and_relevance_rules():
    assert classify_intent("ivf cost in jaipur") == "transactional"
    assert classify_intent("ivf vs iui") == "commercial"
    assert classify_intent("why ivf fails") == "informational"
    assert is_relevant("standing desks for home", "standing desk")  # plural-insensitive
    assert not is_relevant("sitting chair", "standing desk")


def test_clusters_cover_every_keyword_once():
    result = run_engine()
    clustered = [kw for c in result["clusters"] for kw in c["keywords"]]
    assert sorted(clustered) == sorted(k["keyword"] for k in result["keywords"])
    assert all(c["topic"] not in {"hai", "kya"} for c in cluster_keywords([], "ivf"))


def test_calendar_respects_cadence_and_channel_fit():
    google = run_engine(region="Jaipur")
    youtube = run_engine(source="youtube")
    cadence = {"blog": 3, "youtube": 2, "instagram": 0, "facebook": 0, "gbp": 1}
    plan = build_calendar("2026-12", "ivf", google, youtube, cadence, "Jaipur")

    assert len(month_slots("2026-12", 3)) == 13  # Dec 2026: 4 Mon + 5 Wed + 4 Fri
    assert "instagram" not in plan["summary"]
    for channel, per_week in cadence.items():
        if per_week:
            keyword_items = [i for i in plan["items"] if i["channel"] == channel and i["kind"] == "keyword"]
            assert len(keyword_items) <= len(month_slots("2026-12", per_week, _DAY_OFFSET[channel]))
    weekdays = {(c, i["date"]) for i in plan["items"] for c in [i["channel"]]}
    assert len({d for _, d in weekdays}) > len(month_slots("2026-12", 3)), "channels spread over different days"
    days = [i["date"] for i in plan["items"]]
    assert days == sorted(days) and all(d.startswith("2026-12-") for d in days)
    keywords_per_channel = {}
    for item in plan["items"]:
        if item["kind"] == "keyword":
            keywords_per_channel.setdefault(item["channel"], []).append(item["keyword"])
    assert all(len(v) == len(set(v)) for v in keywords_per_channel.values()), "no repeats within a channel"
    assert all(i["source"] in {"google", "calendar"} for i in plan["items"] if i["channel"] == "blog")
    gbp = [i["keyword"] for i in plan["items"] if i["channel"] == "gbp"]
    assert any("jaipur" in k or "near me" in k for k in gbp)


def test_region_plan_skips_other_cities():
    from market_scraper.planner import is_other_city
    assert is_other_city("ivf cost in mumbai", "Jaipur")
    assert not is_other_city("ivf cost in jaipur", "Jaipur")
    assert not is_other_city("ivf cost in mumbai", None)  # All India keeps every city
    assert not is_other_city("ivf centre in navi mumbai", "Navi Mumbai")


def test_titles():
    assert suggest_title("blog", "Blog post", "ivf vs iui") == "IVF vs IUI: Key Differences Explained"
    assert suggest_title("blog", "Blog post", "why ivf fails") == "Why IVF Fails?"


def test_plan_and_excel_endpoints():
    client = TestClient(create_app(SETTINGS, transport=httpx.MockTransport(suggest)))
    res = client.post("/api/plan", json={"keyword": "IVF", "country": "in", "region": "Jaipur", "month": "2026-12",
                                         "cadence": {"blog": 2, "instagram": 3, "youtube": 1}})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["research"]["region"] == "Jaipur" and data["plan"]["total_items"] > 0

    xlsx = client.post("/api/plan/xlsx", json={"plan": data["plan"], "keywords": data["research"]["keywords"],
                                               "clusters": data["research"]["clusters"]})
    assert xlsx.status_code == 200
    wb = load_workbook(BytesIO(xlsx.content))
    assert {"Calendar", "Website blog", "Keywords", "Topic clusters"} <= set(wb.sheetnames)
    assert wb["Calendar"].max_row == data["plan"]["total_items"] + 1


def test_excel_never_writes_formulas():
    client = TestClient(create_app(SETTINGS))
    plan = {"seed": "x", "month": "2026-12", "channels": {"blog": "Website blog"},
            "items": [{"date": "2026-12-01", "channel": "blog", "title": "=HYPERLINK(\"http://evil\")", "keyword": "x"}]}
    wb = load_workbook(BytesIO(client.post("/api/plan/xlsx", json={"plan": plan}).content))
    cell = wb["Calendar"]["E2"]
    assert cell.data_type == "s" and cell.value.startswith("=")


def test_plan_validation():
    client = TestClient(create_app(SETTINGS))
    bad = [{"keyword": "x", "month": "2026-13"}, {"keyword": "x", "month": "2026-12", "region": "<script>"},
           {"keyword": "x", "month": "2026-12", "cadence": {"tiktok": 2}},
           {"keyword": "x", "month": "2026-12", "cadence": {"blog": 0}}]
    for body in bad:
        assert client.post("/api/plan", json=body).status_code == 422, body


def test_searches_are_saved_listed_and_deleted(tmp_path):
    client = TestClient(create_app(SETTINGS, transport=httpx.MockTransport(suggest), store_dir=tmp_path))
    res = client.post("/api/keywords", json={"keyword": "ivf", "country": "in", "region": "Jaipur"})
    assert res.status_code == 200, res.text
    search_id = res.json()["id"]
    listed = client.get("/api/searches").json()["searches"]
    assert listed[0]["id"] == search_id and listed[0]["keyword"] == "ivf" and listed[0]["region"] == "Jaipur"
    assert "keywords" not in listed[0], "the history list carries summaries only"

    # survives a restart
    reopened = TestClient(create_app(SETTINGS, store_dir=tmp_path))
    assert reopened.get(f"/api/searches/{search_id}").json()["total_keywords"] == res.json()["total_keywords"]

    assert reopened.get("/api/searches/..%2F..%2Fsecrets").status_code == 404
    assert reopened.get("/api/searches/" + "0" * 32).status_code == 404
    assert reopened.delete(f"/api/searches/{search_id}").status_code == 204
    assert reopened.get(f"/api/searches/{search_id}").status_code == 404
    assert reopened.get("/api/searches").json()["searches"] == []


def test_regional_calendar_adds_state_events_languages_and_nmc_reframes():
    from market_scraper.regional import nmc_flags, resolve_place

    assert resolve_place("Salt Lake Kolkata") == {"area": "Salt Lake", "city": "Kolkata", "state": "West Bengal"}
    assert resolve_place("Jaipur")["state"] == "Rajasthan"
    assert resolve_place("Maharashtra")["state"] == "Maharashtra"
    assert resolve_place("Some Village")["state"] is None
    assert nmc_flags("ivf success rate in mumbai") and nmc_flags("best ivf doctor")
    assert not nmc_flags("how ivf works")

    google = run_engine(region="Jaipur")
    cadence = {"blog": 2, "youtube": 0, "instagram": 2, "facebook": 1, "gbp": 1}
    plan = build_calendar("2026-12", "ivf", google, None, cadence, "Jaipur")
    assert plan["place"]["state"] == "Rajasthan" and plan["languages"][0] == "Hindi"
    events = {i["keyword"] for i in plan["items"] if i["kind"] == "event"}
    assert "Christmas" in events and "Hornbill Festival begins" not in events  # Nagaland-only
    assert all(i["channel"] in {"instagram", "facebook"} for i in plan["items"]
               if i["kind"] == "event" and i["event"]["kind"] != "health")
    for item in plan["items"]:
        if item["kind"] == "keyword" and item["nmc"]["flags"]:
            assert not nmc_flags(item["title"]), item["title"]
        assert not any(h in {"#best", "#success", "#top"} for h in item["hashtags"])

    us = build_calendar("2026-12", "ivf", google, None, cadence, None, country="us")
    assert not any(i["kind"] == "event" for i in us["items"]) and us["languages"] == []
