"""Territories, regional research loop, campaign planner, Google Trends parsing and open queries (all offline)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from market_scraper import Settings
from market_scraper.analysis import open_queries
from market_scraper.campaign import (LIMITS, build_campaign, campaign_trends, check_ads, check_sources,
                                     compact_trends, pick_trend_keywords)
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


def test_open_queries_carry_fetched_answers_under_their_question():
    recs = [
        Record("quora", "https://www.quora.com/Is-IVF-painful", "Is IVF painful?", "", 5.0, "question"),
        Record("quora", "https://www.quora.com/Is-IVF-painful/answer/A", "Is IVF painful?", "Mild cramps.", 2.0,
               "answer", author="A", metrics={"upvotes": 7, "question_answer_count": 12,
                                              "question_url": "https://www.quora.com/Is-IVF-painful"}),
        # SERP link and API permalink differ in host and slug but are the same thread
        Record("reddit", "https://www.reddit.com/r/IVF/comments/abc12/does_it_hurt/", "Does egg retrieval hurt?",
               "", 4.0, "post"),
        Record("reddit", "https://www.reddit.com/r/IVF/comments/abc12/x/c1", "r/IVF: Does egg retrieval hurt?",
               "Sedated, felt nothing.", 1.0, "comment",
               metrics={"score": 3, "question_url": "https://old.reddit.com/r/IVF/comments/abc12/"}),
        Record("google", "https://google.com/search?q=ivf", "What is the IVF success rate?", "Around 30-40%.", 3.0,
               "people_also_ask"),
        Record("quora", "https://www.quora.com/Blocked-page", "Can IVF fail?", "", 3.0, "question"),
    ]
    by_q = {q["question"]: q for q in open_queries(recs)}
    painful = by_q["Is IVF painful?"]
    assert painful["answers"][0]["text"] == "Mild cramps." and painful["answers"][0]["votes"] == 7
    assert painful["existing_answers"] == 12, "the page's own answer count fills a missing listing count"
    assert by_q["Does egg retrieval hurt?"]["answers"][0]["text"] == "Sedated, felt nothing."
    assert by_q["What is the IVF success rate?"]["answers"][0]["source"] == "Google snippet"
    assert by_q["Can IVF fail?"]["answers"] == [] and by_q["Can IVF fail?"]["answer_key"]


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


# ---------------------------------------------------------------- three data sources: keywords, Trends, social

STUDIO = [{"keyword": "ivf cost", "group": "query", "label": "MOFU", "position": 1},
          {"keyword": "ivf in delhi", "group": "local", "label": "Local", "position": 2},
          {"keyword": "indira ivf delhi", "group": "query", "brand": "own", "position": 3},
          {"keyword": "nova ivf", "group": "query", "brand": "competitor", "position": 1},
          {"keyword": "how long does ivf take step by step", "group": "questions", "position": 1},
          {"keyword": "iui", "group": "comparisons", "label": "TOFU", "position": 4}]


def trends_handler(values_per_keyword: list[list[int]]):
    """Google Trends mock: one series per keyword and a rising related search for 'ivf cost'."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/trends/api/explore":
            return httpx.Response(200, text=")]}'\n" + json.dumps({"widgets": [
                {"id": "TIMESERIES", "token": "t1", "request": {}},
                {"id": "RELATED_QUERIES_1", "token": "t3", "request": {"restriction": {
                    "complexKeywordsRestriction": {"keyword": [{"value": "ivf cost"}]}}}}]}))
        if path.endswith("/multiline"):
            n = len(values_per_keyword[0]) if values_per_keyword else 0
            return httpx.Response(200, text=")]}',\n" + json.dumps({"default": {"timelineData": [
                {"formattedTime": f"w{i}", "value": [v[i] for v in values_per_keyword]} for i in range(n)]}}))
        if path.endswith("/relatedsearches"):
            return httpx.Response(200, text=")]}',\n" + json.dumps({"default": {"rankedList": [
                {"rankedKeyword": [{"query": "ivf cost in delhi", "formattedValue": "100"}]},
                {"rankedKeyword": [{"query": "ivf cost emi", "formattedValue": "+250%"}]}]}}))
        return httpx.Response(200, text="<html></html>")
    return handler


def test_trend_keywords_skip_brands_questions_and_local_phrases():
    assert pick_trend_keywords(STUDIO, "IVF") == ["ivf", "iui", "ivf cost"]
    assert len(pick_trend_keywords([{"keyword": f"term {i}"} for i in range(9)], "ivf")) == 5


def test_campaign_trends_success_gives_direction_change_and_rising_searches():
    handler = trends_handler([[50] * 6, [10] * 6, [20, 30, 40, 60, 80, 100]])
    result, error = asyncio.run(campaign_trends(STUDIO, "Delhi", "ivf", transport=httpx.MockTransport(handler)))
    assert error is None and result["geo"] == "IN-DL" and result["timeframe"] == "quarter"
    rows = {r["keyword"]: r for r in compact_trends(result)}
    assert rows["ivf cost"]["direction"] == "rising" and rows["ivf cost"]["change_pct"] > 100
    assert rows["ivf"]["direction"] == "steady"
    assert rows["ivf cost"]["rising_related_searches"] == ["ivf cost emi (+250%)"]


def test_campaign_trends_failure_and_missing_data_never_raise(monkeypatch):
    from market_scraper import trends

    monkeypatch.setattr(trends, "RETRY_DELAYS", (0.0, 0.0))

    def blocked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429) if request.url.path == "/trends/api/explore" else httpx.Response(200, text="")

    result, error = asyncio.run(campaign_trends(STUDIO, "Delhi", "ivf", transport=httpx.MockTransport(blocked)))
    assert result is None and "rate-limiting" in error

    empty = trends_handler([])  # small districts: Trends answers, but with no series
    result, error = asyncio.run(campaign_trends(STUDIO, None, "ivf", transport=httpx.MockTransport(empty)))
    assert result is None and "no Google Trends data" in error and "India" in error

    assert asyncio.run(campaign_trends([], None, "")) == (None, "no short keywords to look up")


def slot(date, cls, keyword, built_from, title="t"):
    return {"date": date, "week": 1, "format": "Reel", "title": title, "theme": "theme", "target_keyword": keyword,
            "regional_hook": "h", "why_now": "w", "nmc_class": cls, "language": "Hinglish",
            "built_from": built_from, "deliverable": deliverable()}


def bf(keyword="", trend="", social=""):
    return {"keyword_source": keyword, "trend_signal": trend, "social_insight": social}


STRATEGY = {"objective": "o", "audience": "a", "regional_hook": "r", "language_plan": "l", "tone": "t",
            "key_themes": [], "posting_guidance": "p", "measurement": []}


def test_campaign_sends_all_three_sources_and_checks_the_plan_uses_them():
    plan = {"strategy": STRATEGY, "ad_groups": [], "items": [
        slot("2026-12-02", "A", "ivf cost", bf("ivf cost", "ivf cost rising +400%", "100% success guaranteed?")),
        slot("2026-12-09", "B", "ivf hospital timings", bf(), title="Off list"),
        slot("2026-12-25", "C", "", bf(), title="Christmas wishes")]}
    stream = FakeStream(plan)
    client = block(beta=block(messages=block(stream=stream)))
    handler = trends_handler([[50] * 6, [10] * 6, [20, 30, 40, 60, 80, 100]])
    trends_result, _ = asyncio.run(campaign_trends(STUDIO, "Delhi", "ivf", transport=httpx.MockTransport(handler)))
    social = {"query": "IVF cost", "run_at": "2026-11-08T10:00:00Z",
              "open_queries": [f"question {i}?" for i in range(50)], "pain_points": [f"pain {i}" for i in range(15)],
              "trending_topics": ["emi", "loan"], "sentiment": {"average": -0.2, "positive": 1, "neutral": 2, "negative": 3}}
    out = asyncio.run(build_campaign(Settings(), territory_from({"state": "Delhi"}), "2026-12", "instagram", 1,
                                     {"brief": BRIEF}, STUDIO, None, client=client, keyword_origin="studio",
                                     trends=trends_result, social=social))

    sent = json.loads(stream.kwargs["messages"][0]["content"].split("\n\n", 1)[1])
    assert sent["keyword_origin"] == "Keyword Studio results"
    assert not any("nova" in k for k in sent["keyword_research"]), "competitor brands stay filtered"
    assert sent["keyword_research"][0].startswith("ivf in delhi"), "local terms stay first"
    assert {r["keyword"] for r in sent["google_trends"]["keywords"]} == {"ivf", "iui", "ivf cost"}
    assert len(sent["social_listening"]["open_questions"]) == LIMITS["questions"]
    assert len(sent["social_listening"]["pain_points"]) == LIMITS["pain_points"]
    schema_item = stream.kwargs["output_config"]["format"]["schema"]["properties"]["items"]["items"]
    assert "built_from" in schema_item["required"]
    assert schema_item["properties"]["built_from"]["additionalProperties"] is False

    issues = [(c["where"], c["issue"]) for c in out["source_check"]]
    assert ("2026-12-09 Off list", "target keyword is not in the keyword research") in issues
    assert ("2026-12-09 Off list", "built on neither a Trends signal nor a social insight") in issues
    assert not any("Christmas" in w for w, _ in issues), "Class C greetings are exempt"
    assert not any(w == "Plan" for w, _ in issues), "every source was available and used"
    assert out["auto_check"] == [], "quotes in built_from are audience data, not copy"
    assert out["data_used"] == {"keywords": 5, "keyword_origin": "studio", "trends": 3, "trends_error": None,
                                "trends_geo": "IN-DL", "social_questions": 30, "pain_points": 10,
                                "trending_topics": 2, "social_run": "IVF cost",
                                "social_run_at": "2026-11-08T10:00:00Z"}


def test_source_check_flags_ad_keywords_and_missing_or_unused_sources():
    plan = {"items": [slot("2026-12-02", "B", "ivf cost", bf("ivf cost"))],
            "ad_groups": [{"name": "IVF cost", "keywords": ["[ivf cost]", '"ivf in delhi"', "+cheap +ivf"],
                           "built_from": bf("ivf cost")}]}
    keywords = ["ivf cost [MOFU]", "ivf in delhi [Local]"]
    checks = check_sources(plan, keywords, [], {}, "Google Trends is rate-limiting requests right now.")
    plan_notes = [c["issue"] for c in checks if c["where"] == "Plan"]
    assert any("Google Trends was not used (Google Trends is rate-limiting" in n for n in plan_notes)
    assert any("No social listening run" in n for n in plan_notes)
    assert [c["text"] for c in checks if c["where"] == "IVF cost keywords"] == ["+cheap +ivf"], \
        "match-type symbols are ignored when matching research keywords"
    assert not any("neither" in c["issue"] for c in checks), "no Trends or social data: nothing to cite"

    unused = check_sources({"items": [slot("2026-12-02", "A", "ivf cost", bf())], "ad_groups": []},
                           keywords, [{"keyword": "ivf"}], {"open_questions": ["q?"]})
    notes = {c["issue"] for c in unused if c["where"] == "Plan"}
    assert notes == {"Keyword research was sent but no slot says it used it.",
                     "Google Trends data was sent but no slot uses it.",
                     "Social listening insights were sent but no slot uses them."}
    assert check_sources({"items": [], "ad_groups": []}, [], [], {}, None)[0]["issue"].startswith("No keyword research")


def suggest_or_trends(request: httpx.Request) -> httpx.Response:
    """Google Suggest answers; Google Trends is rate-limited."""
    if request.url.host == "suggestqueries.google.com":
        q = request.url.params.get("q", "")
        return httpx.Response(200, content=json.dumps([q, [f"{q} cost", f"{q} guide"]]).encode())
    if request.url.path == "/trends/api/explore":
        return httpx.Response(429)
    return httpx.Response(200, text="<html></html>")


def test_campaign_endpoint_prefers_studio_keywords_and_survives_trends_failure(monkeypatch):
    import server
    from market_scraper import trends

    seen = {}

    async def fake_regional(settings, territory, month, channels, refresh=False):
        return {"brief": BRIEF, "territory": territory.to_dict(), "month": month, "sources": []}

    async def fake_build(settings, territory, month, channel, per_week, regional, keywords, questions, topic, **kw):
        seen.update(keywords=keywords, **kw)
        return {"plan": {"items": [], "ad_groups": []}}

    monkeypatch.setattr(trends, "RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(server, "regional_brief", fake_regional)
    monkeypatch.setattr(server, "build_campaign", fake_build)
    settings = Settings(max_retries=1, backoff_base=0.001, backoff_max=0.01, keyword_rps=0)
    client = TestClient(server.create_app(settings, transport=httpx.MockTransport(suggest_or_trends)))
    base = {"territory": {"state": "Delhi"}, "month": "2026-12", "channels": ["instagram"], "channel": "instagram"}

    res = client.post("/api/campaign", json={**base, "keywords": STUDIO[:3], "social": {
        "query": "IVF cost", "open_queries": ["  is ivf painful?  "], "trending_topics": ["x" * 200]}})
    assert res.status_code == 200, res.text
    assert seen["keyword_origin"] == "studio"
    assert [k["keyword"] for k in seen["keywords"]] == ["ivf cost", "ivf in delhi", "indira ivf delhi"]
    assert seen["trends"] is None and "rate-limiting" in seen["trends_error"], "Trends failure never fails the plan"
    assert seen["social"]["open_queries"] == ["is ivf painful?"] and len(seen["social"]["trending_topics"][0]) == 60

    res = client.post("/api/campaign", json=base)  # no Studio results: fresh search for the seed
    assert res.status_code == 200, res.text
    assert seen["keyword_origin"] == "auto" and seen["keywords"] and seen["social"] is None
    assert res.json()["keyword_count"] == len(seen["keywords"])

    too_many = client.post("/api/campaign", json={**base, "keywords": [{"keyword": "ivf"}] * 301})
    bad_brand = client.post("/api/campaign", json={**base, "keywords": [{"keyword": "ivf", "brand": "rival"}]})
    assert too_many.status_code == 422 and bad_brand.status_code == 422
