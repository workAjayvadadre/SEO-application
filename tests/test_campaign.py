"""Territories, regional research loop, campaign planner, Google Trends parsing and open queries (all offline)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from market_scraper import Settings
from market_scraper.analysis import open_queries
from market_scraper.campaign import build_campaign, check_ads
from market_scraper.geo import TerritoryError, territory_from
from market_scraper.models import Record
from market_scraper import regional_research
from market_scraper.trends import google_trends

BRIEF = {"territory_summary": "Delhi", "languages": {"primary": "Hindi", "others": ["English"], "script": "Devanagari",
                                                    "tone_notes": "Hinglish"},
         "events": [{"date": "2026-12-25", "name": "Christmas", "type": "festival", "impact": "medium", "where": "Delhi",
                     "why_it_matters": "x", "content_angle": "greeting", "verify": False, "source": "https://a"}],
         "seasonal_context": ["winter smog"], "cultural_nuances": [], "health_context": [], "audience_insights": [],
         "channel_notes": [], "local_search_terms": [], "sensitivities": []}


def block(**kw):
    return SimpleNamespace(**kw)


def test_territory_validation_follows_the_lgd_hierarchy():
    t = territory_from({"state": "Maharashtra", "district": "Pune", "subdistrict": "Baramati", "village": "Malegaon"})
    assert t.level == "village" and t.zone == "West India" and t.states == ("Maharashtra",)
    assert t.label == "Malegaon, Baramati, Pune, Maharashtra"
    assert len(territory_from({"zone": "South India"}).states) == 7
    with pytest.raises(TerritoryError):
        territory_from({"state": "Delhi", "district": "Pune"})
    with pytest.raises(TerritoryError):
        territory_from({"zone": "South India", "state": "Delhi"})
    with pytest.raises(TerritoryError):
        territory_from({"state": "Maharashtra", "village": "Malegaon"})  # village needs a district


def test_regional_research_continues_after_pause_and_returns_the_submitted_brief(tmp_path, monkeypatch):
    monkeypatch.setattr(regional_research, "CACHE_DIR", tmp_path)
    calls = []
    replies = [
        block(stop_reason="pause_turn", model="m", usage=block(input_tokens=5, output_tokens=5), content=[
            block(type="web_search_tool_result", content=[block(url="https://delhi.gov.in/holidays", title="Holidays")])]),
        block(stop_reason="tool_use", model="m", usage=block(input_tokens=5, output_tokens=5), content=[
            block(type="tool_use", name="submit_regional_brief", input=BRIEF)]),
    ]

    async def create(**kw):
        calls.append(kw)
        return replies[len(calls) - 1]

    client = block(beta=block(messages=block(create=create)))
    t = territory_from({"state": "Delhi"})
    out = asyncio.run(regional_research.regional_brief(Settings(), t, "2026-12", ("instagram",), client=client))
    assert out["brief"]["languages"]["primary"] == "Hindi" and not out["cached"]
    assert out["sources"][0]["url"] == "https://delhi.gov.in/holidays"
    assert calls[0]["tools"][0]["type"] == "web_search_20260209" and calls[0]["tools"][0]["user_location"]["region"] == "Delhi"
    assert len(calls[1]["messages"]) == 2  # resumed with the paused assistant turn, no extra user message
    again = asyncio.run(regional_research.regional_brief(Settings(), t, "2026-12", ("instagram",), client=client))
    assert again["cached"] and len(calls) == 2


class FakeStream:
    def __init__(self, payload):
        self.payload, self.kwargs = payload, None

    def __call__(self, **kw):
        self.kwargs = kw
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return block(stop_reason="end_turn", model="m", usage=block(input_tokens=1, output_tokens=1),
                     content=[block(type="text", text=json.dumps(self.payload))])


def deliverable():
    return {"objective": "o", "key_messages": ["k"], "questions_to_answer": [], "outline": [], "must_include": [],
            "avoid": ["never say best or 100% success"], "visual_direction": "v", "allowed_cta": "none",
            "evidence": [], "kpis": []}


def test_campaign_keeps_dates_in_month_and_flags_ad_copy():
    item = {"week": 1, "format": "Reel", "title": "Winter care", "theme": "t", "target_keyword": "ivf delhi",
            "regional_hook": "h", "why_now": "w", "nmc_class": "A", "language": "Hinglish", "deliverable": deliverable()}
    plan = {"strategy": {"objective": "o", "audience": "a", "regional_hook": "r", "language_plan": "l", "tone": "t",
                         "key_themes": [], "posting_guidance": "p", "measurement": []},
            "items": [{**item, "date": "2026-12-03"}, {**item, "date": "2027-01-02"}],
            "ad_groups": [{"week": 1, "name": "IVF Delhi", "theme": "t", "keywords": ["ivf delhi"],
                           "negative_keywords": ["free", "jobs"], "geo_targets": ["Delhi"], "schedule": "s",
                           "headlines": ["Best IVF Centre in Delhi", "IVF Treatment Information for Couples"],
                           "descriptions": ["Learn about each IVF step."], "landing_page_brief": "l", "nmc_notes": "n"}]}
    stream = FakeStream(plan)
    client = block(beta=block(messages=block(stream=stream)))
    t = territory_from({"state": "Delhi"})
    out = asyncio.run(build_campaign(Settings(), t, "2026-12", "google_ads", 1, {"brief": BRIEF},
                                     [{"keyword": "ivf in delhi", "group": "local", "label": "Local"}], ["is ivf safe?"],
                                     client=client))
    assert [i["date"] for i in out["plan"]["items"]] == ["2026-12-03"]
    assert out["auto_check"] == []  # "avoid" lists name banned words on purpose
    issues = {p["issue"].split(" (")[0] for p in out["ads_check"]}
    assert "superlative / comparative claim" in issues and any("max 30" in p["issue"] for p in out["ads_check"])
    sent = json.loads(stream.kwargs["messages"][0]["content"].split("\n\n", 1)[1])
    assert sent["slots"] == 5 and sent["keyword_research"][0].startswith("ivf in delhi")
    assert check_ads([]) == []


def test_google_trends_parses_explore_widgets():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/trends/api/explore":
            return httpx.Response(200, text=")]}'\n" + json.dumps({"widgets": [
                {"id": "TIMESERIES", "token": "t1", "request": {}},
                {"id": "GEO_MAP", "token": "t2", "request": {}},
                {"id": "RELATED_QUERIES", "token": "t3", "request": {"restriction": {
                    "complexKeywordsRestriction": {"keyword": [{"value": "ivf"}]}}}}]}))
        if path.endswith("/multiline"):
            return httpx.Response(200, text=")]}',\n" + json.dumps({"default": {"timelineData": [
                {"formattedTime": f"w{i}", "value": [v]} for i, v in enumerate([20, 30, 40, 60, 80, 100])]}}))
        if path.endswith("/comparedgeo"):
            return httpx.Response(200, text=")]}',\n" + json.dumps({"default": {"geoMapData": [
                {"geoName": "New Delhi", "value": [100]}]}}))
        if path.endswith("/relatedsearches"):
            return httpx.Response(200, text=")]}',\n" + json.dumps({"default": {"rankedList": [
                {"rankedKeyword": [{"query": "ivf cost", "formattedValue": "100"}]},
                {"rankedKeyword": [{"query": "ivf ipo", "formattedValue": "Breakout"}]}]}}))
        return httpx.Response(200, text="<html></html>")

    out = asyncio.run(google_trends(["ivf"], "Delhi", "year", transport=httpx.MockTransport(handler)))
    assert out["geo"] == "IN-DL" and out["summary"][0]["direction"] == "rising"
    assert out["related"]["ivf"]["rising"][0]["query"] == "ivf ipo" and out["by_region"][0]["region"] == "New Delhi"


def test_open_queries_rank_interest_over_existing_answers():
    recs = [
        Record("quora", "https://q/1", "Is IVF safe after 40?", "", 5.0, "question", metrics={"question_answer_count": 1}),
        Record("reddit", "https://r/1", "How long does IVF take?", "", 9.0, "post", metrics={"num_comments": 40}),
        Record("youtube", "https://y/1", "video", "Can I travel after embryo transfer?", 3.0, "comment"),
        Record("google", "https://g/1", "Best clinics", "", 8.0, "organic"),
    ]
    out = open_queries(recs)
    assert [q["platform"] for q in out][:2] == ["quora", "youtube"]
    assert all(q["platform"] != "google" for q in out)


def test_google_trends_retries_after_rate_limit_then_reports_it(monkeypatch):
    from market_scraper import trends

    monkeypatch.setattr(trends, "RETRY_DELAYS", (0.0, 0.0))
    calls = {"explore": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/trends/api/explore":
            calls["explore"] += 1
            return httpx.Response(429)
        return httpx.Response(200, text="<html></html>")

    with pytest.raises(trends.TrendsError, match="rate-limiting"):
        asyncio.run(google_trends(["ivf"], None, "year", transport=httpx.MockTransport(handler)))
    assert calls["explore"] == 3  # first try + two polite retries
