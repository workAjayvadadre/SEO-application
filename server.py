"""Local web UI for the aggregator.

    python server.py                 # http://127.0.0.1:8000
    python server.py --port 9000 --open

There is no login: anyone who can reach the server can spend your API quota. It binds to
127.0.0.1 by default. Only use --host 0.0.0.0 on a network you trust.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import asyncio
import logging
import os
import re
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from typing import Literal

from market_scraper import ALL_PLATFORMS, Aggregator, SearchRequest, Settings, use_system_certificates
from market_scraper.http import FetchError
from market_scraper.export import keywords_to_answersocrates_xlsx, plan_to_xlsx
from market_scraper.keywords import KeywordEngine, KeywordRequest
from market_scraper.planner import CHANNELS, DEFAULT_CADENCE, build_calendar
from market_scraper.aiquestions import ai_assistant_questions
from market_scraper.campaign import CAMPAIGN_CHANNELS, build_campaign
from market_scraper.geo import TerritoryError, territory_from
from market_scraper.regional import resolve_place
from market_scraper.regional_research import regional_brief
from market_scraper.trends import TrendsError, google_trends
from market_scraper.scriptgen import ScriptGenError, generate_content
from market_scraper.manual_answers import MAX_ANSWER_CHARS, MAX_ANSWERS_PER_QUESTION, ManualAnswers
from market_scraper.answer_writer import AiAnswers, answers_for
from market_scraper.youtube_account import (PendingSignIns, YouTubeAccount, YouTubeAuthError, auth_url,
                                            client_configured, exchange, start_loopback)
from market_scraper.publishers import capabilities
from market_scraper.replies import ReplyError, ReplyQueue, draft_reply
from market_scraper.store import SearchStore

UI_DIR = Path(__file__).parent / "ui"
DATA_DIR = Path(__file__).parent / "data" / "searches"
MANUAL_ANSWERS_FILE = Path(__file__).parent / "data" / "manual_answers.json"
REPLIES_FILE = Path(__file__).parent / "data" / "replies.json"
AI_ANSWERS_FILE = Path(__file__).parent / "data" / "ai_answers.json"
YOUTUBE_ACCOUNT_FILE = Path(__file__).parent / "data" / "youtube_account.json"
_HASHTAG = re.compile(r"^[0-9A-Za-z_]{1,100}$")
_HTTP_URL = r"^https?://"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}

# Scraped content is untrusted. The page renders it as text only, and this CSP is a second line of defence.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    platforms: list[str] = Field(default_factory=lambda: list(ALL_PLATFORMS), min_length=1)
    limit: int = Field(10, ge=1, le=50)
    comments: int = Field(10, ge=0, le=50)
    hashtags: list[str] = Field(default_factory=list, max_length=10)
    # Any place in India: state, district, city, area, tehsil / taluka or village.
    location: str | None = Field(None, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9 .,&'-]*$")
    time_range: Literal["day", "week", "month", "year"] | None = None

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value.strip()

    @field_validator("platforms")
    @classmethod
    def _known_platforms(cls, value: list[str]) -> list[str]:
        unknown = [p for p in value if p not in ALL_PLATFORMS]
        if unknown:
            raise ValueError(f"unknown platform(s): {', '.join(unknown)}")
        return list(dict.fromkeys(value))

    @field_validator("hashtags")
    @classmethod
    def _clean_hashtags(cls, value: list[str]) -> list[str]:
        cleaned = [h.strip().lstrip("#") for h in value if h.strip().lstrip("#")]
        bad = [h for h in cleaned if not _HASHTAG.match(h)]
        if bad:
            raise ValueError(f"hashtags may only contain letters, digits and _: {', '.join(bad)}")
        return cleaned


class KeywordBody(BaseModel):
    keyword: str = Field(min_length=1, max_length=100)
    source: Literal["google", "youtube"] = "google"
    country: str = Field("us", pattern=r"^[A-Za-z]{2}$")
    language: str = Field("en", pattern=r"^[A-Za-z]{2,3}(-[A-Za-z]{2,4})?$")
    include_serp: bool = False
    region: str | None = Field(None, max_length=60, pattern=r"^[A-Za-z][A-Za-z0-9 .&'-]*$")


class PlanBody(KeywordBody):
    month: str = Field(pattern=r"^20\d\d-(0[1-9]|1[0-2])$")
    cadence: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_CADENCE))

    @field_validator("cadence")
    @classmethod
    def _valid_cadence(cls, value: dict[str, int]) -> dict[str, int]:
        unknown = set(value) - set(CHANNELS)
        if unknown:
            raise ValueError(f"unknown channel(s): {', '.join(sorted(unknown))}")
        if any(not 0 <= n <= 7 for n in value.values()):
            raise ValueError("posts per week must be between 0 and 7")
        if not any(value.values()):
            raise ValueError("pick at least one channel")
        return value


class ScriptItem(BaseModel):
    """The calendar card to write. Only these fields reach the model."""
    date: str = Field(pattern=r"^20\d\d-\d\d-\d\d$")
    channel: Literal["blog", "youtube", "instagram", "facebook", "gbp"]
    channel_label: str = Field("", max_length=60)
    format: str = Field(max_length=40)
    keyword: str = Field(min_length=1, max_length=150)
    title: str = Field("", max_length=200)
    intent: str = Field("", max_length=30)
    funnel: str = Field("", max_length=30)
    hashtags: list[str] = Field(default_factory=list, max_length=20)
    event: dict | None = None


class ScriptBody(BaseModel):
    item: ScriptItem
    month: str | None = Field(None, pattern=r"^20\d\d-(0[1-9]|1[0-2])$")
    region: str | None = Field(None, max_length=60, pattern=r"^[A-Za-z][A-Za-z0-9 .&'-]*$")
    country: str = Field("in", pattern=r"^[A-Za-z]{2}$")
    language: str | None = Field(None, max_length=40, pattern=r"^[A-Za-z][A-Za-z ()-]*$")


_PLACE = r"^[A-Za-z][A-Za-z0-9 .,&'()/-]*$"


class TerritoryBody(BaseModel):
    zone: str | None = Field(None, max_length=40, pattern=_PLACE)
    state: str | None = Field(None, max_length=60, pattern=_PLACE)
    district: str | None = Field(None, max_length=80, pattern=_PLACE)
    subdistrict: str | None = Field(None, max_length=80, pattern=_PLACE)
    village: str | None = Field(None, max_length=80, pattern=_PLACE)


class RegionalBody(BaseModel):
    territory: TerritoryBody = Field(default_factory=TerritoryBody)
    month: str = Field(pattern=r"^20\d\d-(0[1-9]|1[0-2])$")
    channels: list[Literal["instagram", "youtube", "facebook", "blog", "gbp", "google_ads"]] = Field(min_length=1, max_length=6)
    refresh: bool = False


class CampaignBody(RegionalBody):
    channel: Literal["instagram", "youtube", "facebook", "blog", "gbp", "google_ads"]
    per_week: int = Field(3, ge=1, le=7)
    topic: str = Field("IVF and fertility care", min_length=2, max_length=80)
    seed: str = Field("ivf", min_length=2, max_length=60)
    questions: list[str] = Field(default_factory=list, max_length=60)

    @field_validator("questions")
    @classmethod
    def _short_questions(cls, value: list[str]) -> list[str]:
        return [q.strip()[:300] for q in value if q.strip()]


class TrendsBody(BaseModel):
    keywords: list[str] = Field(min_length=1, max_length=5)
    state: str | None = Field(None, max_length=60, pattern=_PLACE)
    timeframe: Literal["day", "week", "month", "quarter", "year", "5years"] = "year"

    @field_validator("keywords")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        value = [k.strip()[:80] for k in value if k.strip()]
        if not value:
            raise ValueError("give at least one keyword")
        return value


class ManualAnswersBody(BaseModel):
    """Answers pasted by hand for one open query. An empty list removes them."""
    source_url: str = Field(min_length=1, max_length=2000, pattern=_HTTP_URL)
    question: str = Field(min_length=1, max_length=300)
    answers: list[str] = Field(default_factory=list, max_length=MAX_ANSWERS_PER_QUESTION)

    @field_validator("answers")
    @classmethod
    def _clean_answers(cls, value: list[str]) -> list[str]:
        if any(len(a) > MAX_ANSWER_CHARS for a in value):
            raise ValueError(f"each answer can be at most {MAX_ANSWER_CHARS} characters")
        return [a for a in value if a.strip()]


_PERSON = Field(min_length=2, max_length=80, pattern=r"^[^\x00-\x1f<>]+$")


class ReplyAddBody(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    platform: Literal["google", "reddit", "quora", "youtube", "instagram", "facebook"]
    record_type: str = Field("question", max_length=40)
    source_url: str = Field(min_length=1, max_length=2000, pattern=_HTTP_URL)
    graph_id: str | None = Field(None, max_length=64, pattern=r"^[0-9_]+$")
    context: list[str] = Field(default_factory=list, max_length=10)
    reply: str | None = Field(None, max_length=3000)  # start from a written answer (disclosure line added)


class OpenAnswerItem(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    source_url: str = Field(min_length=1, max_length=2000, pattern=_HTTP_URL)
    platform: str = Field(max_length=20)
    context: list[str] = Field(default_factory=list, max_length=5)


class OpenAnswersBody(BaseModel):
    items: list[OpenAnswerItem] = Field(min_length=1, max_length=30)
    generate: bool = False  # False: only return cached answers (no Claude call)
    refresh: bool = False  # True: rewrite even the cached ones


class ReplyEditBody(BaseModel):
    reply: str = Field(max_length=3000)


class ReplyApproveBody(BaseModel):
    approver: str = _PERSON


class ReplyPostBody(BaseModel):
    by: str = _PERSON
    confirm: Literal[True]  # explicit: the client must say it means to publish


class ReplyMarkPostedBody(BaseModel):
    by: str = _PERSON
    url: str | None = Field(None, max_length=2000, pattern=_HTTP_URL)


class AiQuestionsBody(BaseModel):
    keyword: str = Field(min_length=1, max_length=100)
    country: str = Field("in", pattern=r"^[A-Za-z]{2}$")
    region: str | None = Field(None, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9 .,&'()/-]*$")


class KeywordExportBody(BaseModel):
    results: list[dict] = Field(min_length=1, max_length=4)
    name: str = Field("keywords", max_length=100)


class ExportBody(BaseModel):
    plan: dict
    keywords: list[dict] = Field(default_factory=list, max_length=5000)
    clusters: list[dict] = Field(default_factory=list, max_length=500)


def create_app(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
               allowed_hosts: list[str] | None = None, store_dir: Path | None = None,
               manual_answers_file: Path | None = None, replies_file: Path | None = None,
               ai_answers_file: Path | None = None, youtube_account_file: Path | None = None,
               google_transport: httpx.BaseTransport | None = None) -> FastAPI:
    app = FastAPI(title="Market Scraper", docs_url=None, redoc_url=None, openapi_url=None)
    hosts = allowed_hosts or ["127.0.0.1", "localhost", "[::1]", "testserver"]
    # Blocks DNS-rebinding: a malicious site can't drive this server through the browser.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    searches = asyncio.Semaphore(2)  # protects API quotas from a burst of clicks
    store = SearchStore(store_dir)  # no directory (tests) = in memory only
    manual = ManualAnswers(manual_answers_file)  # no file (tests) = in memory only
    replies = ReplyQueue(settings, replies_file)  # no file (tests) = in memory only
    ai_answers = AiAnswers(ai_answers_file)  # no file (tests) = in memory only
    youtube = YouTubeAccount(youtube_account_file)  # channel connected from the UI
    sign_ins = PendingSignIns()
    if youtube.refresh_token:  # a UI-connected channel wins over YOUTUBE_OAUTH_REFRESH_TOKEN in .env
        replies.settings = replace(settings, youtube_oauth_refresh_token=youtube.refresh_token)
    writing = asyncio.Semaphore(1)  # one batch of Claude answers at a time

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        """Another website open in the browser must never be able to post replies through this server."""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin:
                origin_host = (urlsplit(origin).hostname or "").lower()
                own_host = (urlsplit(f"//{request.headers.get('host', '')}").hostname or "").lower()
                # Same site (incl. the public name behind nginx) or a listed host; "*" never counts here.
                trusted = {h.strip("[]").lower() for h in hosts if h != "*"} | {own_host}
                if origin_host not in trusted:
                    return JSONResponse({"detail": "Cross-origin request refused."}, status_code=403)
        return await call_next(request)

    def with_manual_answers(payload: dict) -> dict:
        for q in payload.get("insights", {}).get("open_queries", []):
            q["manual_answers"] = manual.get(q.get("answer_key", ""))
            q["ai_answer"] = ai_answers.get(q.get("answer_key", ""))
        return payload

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            # Revalidate on every load (cheap 304 via ETag) so an update never runs against stale JS.
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")

    @app.get("/api/platforms")
    async def platforms() -> dict:
        availability = Aggregator(settings, transport=transport).availability()
        return {
            "platforms": [{"name": name, "available": ok, "reason": reason}
                          for name, (ok, reason) in availability.items()],
            "defaults": {"limit": settings.default_limit, "comments": settings.default_comments,
                         "country": settings.search_country, "language": settings.search_language},
            "serp_configured": bool(settings.serp_provider),
            "content_writer": bool(settings.anthropic_api_key),
            "brand_name": settings.brand_name,
            "reply_posting": capabilities(replies.settings),
            "campaign_channels": {k: v["label"] for k, v in CAMPAIGN_CHANNELS.items()},
        }

    @app.post("/api/keywords")
    async def keywords(body: KeywordBody) -> dict:
        request = KeywordRequest(keyword=body.keyword, source=body.source, country=body.country.lower(),
                                 language=body.language.lower(), include_serp=body.include_serp, region=body.region)
        async with searches:
            try:
                result = await KeywordEngine(settings, transport=transport).run(request)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except FetchError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        search_id = store.save(result)
        return store.get(search_id) or result

    @app.get("/api/searches")
    async def list_searches() -> dict:
        return {"searches": store.list()}

    @app.get("/api/searches/{search_id}")
    async def get_search(search_id: str) -> dict:
        found = store.get(search_id)
        if not found:
            raise HTTPException(status_code=404, detail="search not found")
        return found

    @app.delete("/api/searches/{search_id}", status_code=204)
    async def delete_search(search_id: str) -> Response:
        if not store.delete(search_id):
            raise HTTPException(status_code=404, detail="search not found")
        return Response(status_code=204)

    @app.post("/api/plan")
    async def plan(body: PlanBody) -> dict:
        """Google research (with region) + YouTube research, then a channel-wise monthly calendar."""
        base = dict(country=body.country.lower(), language=body.language.lower())
        engine = KeywordEngine(settings, transport=transport)
        async with searches:
            try:
                google = await engine.run(KeywordRequest(keyword=body.keyword, include_serp=body.include_serp,
                                                         region=body.region, **base))
                youtube = None
                if body.cadence.get("youtube") or body.cadence.get("instagram"):
                    try:
                        youtube = await engine.run(KeywordRequest(keyword=body.keyword, source="youtube", **base))
                    except FetchError:
                        youtube = None  # the calendar still works from Google ideas alone
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except FetchError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        calendar_plan = build_calendar(body.month, google["keyword"], google, youtube, body.cadence, body.region,
                                       country=body.country)
        return {"research": google, "youtube": youtube, "plan": calendar_plan}

    @app.post("/api/brief")
    async def brief(body: ScriptBody) -> dict:
        """Deliverable brief (objective, hooks, outline, must-include, visual direction, NMC checklist) for one card."""
        item = body.item.model_dump()
        if item.get("event"):
            ev = item["event"]
            item["event"] = {k: str(ev.get(k, ""))[:200] for k in ("date", "name", "kind", "rule")}
        try:
            return await generate_content(settings, item, month=body.month, region=body.region,
                                          country=body.country, language=body.language)
        except ScriptGenError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/keywords/xlsx")
    async def keywords_xlsx(body: KeywordExportBody) -> Response:
        """Keyword research in AnswerSocrates' column layout (Type, Modifier, Question, Keyword Label, ...)."""
        if sum(len(r.get("keywords", [])) for r in body.results) > 20000:
            raise HTTPException(status_code=400, detail="too many keywords")
        content = keywords_to_answersocrates_xlsx(body.results)
        name = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-") or "keywords"
        return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})

    def _territory(body: TerritoryBody):
        try:
            return territory_from(body.model_dump())
        except TerritoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/ai-questions")
    async def ai_questions(body: AiQuestionsBody) -> dict:
        """Questions people typically ask AI assistants about the keyword (estimated by Claude)."""
        try:
            return await ai_assistant_questions(settings, body.keyword, body.country.lower(), body.region)
        except ScriptGenError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/trends")
    async def trends(body: TrendsBody) -> dict:
        """Google Trends: relative interest over time, related searches and interest by region."""
        try:
            return await google_trends(body.keywords, body.state, body.timeframe)
        except TrendsError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/regional")
    async def regional(body: RegionalBody) -> dict:
        """Live web research on a territory for one month (Claude + web search), cached 14 days."""
        territory = _territory(body.territory)
        try:
            return await regional_brief(settings, territory, body.month, tuple(body.channels), refresh=body.refresh)
        except ScriptGenError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/campaign")
    async def campaign(body: CampaignBody) -> dict:
        """Regional research (cached) + local keyword research + Claude plan with a brief per slot."""
        territory = _territory(body.territory)
        try:
            regional_result = await regional_brief(settings, territory, body.month, tuple(body.channels))
        except ScriptGenError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        place = territory.subdistrict or territory.district or territory.state
        source = "youtube" if body.channel == "youtube" else "google"
        async with searches:
            try:
                research = await KeywordEngine(settings, transport=transport).run(KeywordRequest(
                    keyword=body.seed, source=source, country="in", language="en",
                    region=place if place and re.match(r"^[A-Za-z][A-Za-z0-9 .&'-]*$", place) else None))
            except (ValueError, FetchError) as exc:
                research = {"keywords": [], "warnings": [str(exc)]}
        try:
            result = await build_campaign(settings, territory, body.month, body.channel, body.per_week,
                                          regional_result, research.get("keywords", []), body.questions, body.topic)
        except ScriptGenError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        result["regional"] = regional_result
        result["keyword_count"] = len(research.get("keywords", []))
        return result

    @app.post("/api/plan/xlsx")
    async def plan_xlsx(body: ExportBody) -> Response:
        if len(body.plan.get("items", [])) > 2000:
            raise HTTPException(status_code=400, detail="plan too large")
        content = plan_to_xlsx(body.plan, body.keywords, body.clusters)
        name = re.sub(r"[^a-z0-9]+", "-", str(body.plan.get("seed", "plan")).lower()).strip("-") or "plan"
        month = re.sub(r"[^0-9-]", "", str(body.plan.get("month", "")))
        return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="content-plan-{name}-{month}.xlsx"'})

    @app.post("/api/search")
    async def search(body: SearchBody) -> dict:
        query, geo = body.query, None
        if body.location:
            place = resolve_place(body.location.replace(",", " "))
            # Every platform searches "<query> <place>"; Google also gets a canonical location when known.
            if body.location.lower() not in query.lower():
                query = f"{query} {body.location.replace(',', ' ')}"
            if place.get("state"):
                geo = ",".join(p for p in (place.get("city"), place["state"], "India") if p)
        request = SearchRequest(query=query, limit=body.limit, comments_per_item=body.comments,
                                hashtags=tuple(body.hashtags), geo_location=geo, time_range=body.time_range)
        async with searches:
            try:
                result = await Aggregator(settings, transport=transport).run(request, body.platforms)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return with_manual_answers(result)

    @app.get("/api/manual-answers")
    async def list_manual_answers() -> dict:
        """Every pasted answer, keyed by answer_key (also used to refresh results files opened from disk)."""
        return {"answers": {k: v["answers"] for k, v in manual.all().items()}}

    @app.put("/api/manual-answers")
    async def save_manual_answers(body: ManualAnswersBody) -> dict:
        return manual.set(body.source_url, body.question, body.answers)

    @app.post("/api/open-answers")
    async def open_answers(body: OpenAnswersBody) -> dict:
        """Claude-written answers for open queries, keyed by answer_key. generate=False only reads the cache."""
        items = [i.model_dump() for i in body.items]
        try:
            if body.generate:
                async with writing:
                    found = await answers_for(settings, ai_answers, items, generate=True, refresh=body.refresh)
            else:
                found = await answers_for(settings, ai_answers, items, generate=False)
        except ScriptGenError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"answers": found, "writer": bool(settings.anthropic_api_key)}

    # ------------------------------------------------------------ YouTube account (connect from the UI)

    def finish_youtube(query: dict[str, str]) -> str:
        """Complete a sign-in from Google's callback; returns where to send the browser."""
        pending = sign_ins.take(query.get("state", ""))
        if not pending:
            return "/?youtube=expired#replies"
        redirect, return_to = pending
        if query.get("error") or not query.get("code"):
            return f"{return_to}/?youtube=cancelled#replies"
        try:
            got = exchange(settings, query["code"], redirect, transport=google_transport)
        except YouTubeAuthError as exc:
            logging.getLogger("server").warning("YouTube connect failed: %s", exc)
            return f"{return_to}/?youtube=failed#replies"
        youtube.save(got["refresh_token"], got["email"])
        replies.settings = replace(replies.settings, youtube_oauth_refresh_token=got["refresh_token"])
        return f"{return_to}/?youtube=connected#replies"

    @app.get("/api/youtube/account")
    async def youtube_account() -> dict:
        connected_in_env = bool(settings.youtube_oauth_refresh_token) and not youtube.refresh_token
        return {**youtube.status(), "connected": bool(replies.settings.youtube_oauth_refresh_token),
                "source": "env" if connected_in_env else "app", "client_configured": client_configured(settings)}

    @app.get("/api/youtube/connect", include_in_schema=False)
    async def youtube_connect(request: Request) -> Response:
        if not client_configured(settings):
            return RedirectResponse("/?youtube=no-client#replies", status_code=303)
        base = str(request.base_url).rstrip("/")
        own_callback = f"{base}/api/youtube/callback"
        redirect = settings.youtube_oauth_redirect_url or own_callback
        cb, own = urlsplit(redirect), urlsplit(own_callback)
        if (cb.hostname, cb.port) != (own.hostname, own.port) or cb.path != own.path:
            if cb.hostname not in ("localhost", "127.0.0.1") or cb.scheme != "http":
                return RedirectResponse("/?youtube=bad-redirect#replies", status_code=303)
            try:  # registered for another local port (e.g. the marketing app): answer there for this sign-in
                start_loopback(redirect, finish_youtube)
            except YouTubeAuthError:
                return RedirectResponse("/?youtube=port-busy#replies", status_code=303)
        state = sign_ins.new(redirect, base)
        return RedirectResponse(auth_url(settings, redirect, state), status_code=303)

    @app.get("/api/youtube/callback", include_in_schema=False)
    async def youtube_callback(request: Request) -> Response:
        location = await asyncio.to_thread(finish_youtube, dict(request.query_params))
        return RedirectResponse(location, status_code=303)

    @app.delete("/api/youtube/account", status_code=204)
    async def youtube_disconnect() -> Response:
        youtube.clear()
        replies.settings = replace(replies.settings, youtube_oauth_refresh_token=settings.youtube_oauth_refresh_token)
        return Response(status_code=204)

    # ------------------------------------------------------------ reply queue (ORM)

    def reply_call(fn, *args):
        try:
            return fn(*args)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Reply not found.") from exc
        except ReplyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/replies")
    async def list_replies() -> dict:
        return {"items": replies.list(), "capabilities": capabilities(replies.settings), "brand_name": settings.brand_name,
                "daily_limit": settings.reply_daily_limit, "drafting": bool(settings.anthropic_api_key)}

    @app.post("/api/replies")
    async def add_reply(body: ReplyAddBody) -> dict:
        return replies.add(question=body.question, platform=body.platform, record_type=body.record_type,
                           source_url=body.source_url, graph_id=body.graph_id, context=body.context,
                           reply=body.reply)

    @app.post("/api/replies/{item_id}/draft")
    async def draft_reply_route(item_id: str) -> dict:
        item = reply_call(replies.get, item_id)
        try:
            drafted = await draft_reply(settings, item)
        except ScriptGenError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return reply_call(replies.set_draft, item_id, drafted)

    @app.put("/api/replies/{item_id}")
    async def edit_reply(item_id: str, body: ReplyEditBody) -> dict:
        return reply_call(replies.edit, item_id, body.reply)

    @app.post("/api/replies/{item_id}/approve")
    async def approve_reply(item_id: str, body: ReplyApproveBody) -> dict:
        return reply_call(replies.approve, item_id, body.approver.strip())

    @app.post("/api/replies/{item_id}/post")
    async def post_reply(item_id: str, body: ReplyPostBody) -> dict:
        try:
            return await replies.post(item_id, body.by.strip(), transport=transport)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Reply not found.") from exc
        except ReplyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/replies/{item_id}/mark-posted")
    async def mark_reply_posted(item_id: str, body: ReplyMarkPostedBody) -> dict:
        return reply_call(replies.mark_posted, item_id, body.url, body.by.strip())

    @app.delete("/api/replies/{item_id}", status_code=204)
    async def delete_reply(item_id: str) -> Response:
        reply_call(replies.delete, item_id)
        return Response(status_code=204)

    app.mount("/static", StaticFiles(directory=UI_DIR), name="static")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Market Scraper web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--open", action="store_true", help="open the UI in your browser")
    args = parser.parse_args()
    use_system_certificates()  # corporate HTTPS inspection: trust the Windows certificate store

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs incl. API keys

    allowed = None
    # Behind a reverse proxy (nginx) the app binds to 127.0.0.1 but receives the public Host header.
    extra_hosts = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]
    if extra_hosts:
        allowed = ["127.0.0.1", "localhost", "[::1]", *extra_hosts]
    if args.host not in _LOOPBACK:
        logging.warning("Listening on %s with no authentication. Anyone on this network can use your API keys.",
                        args.host)
        allowed = ["*"]

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(f"Keyword Studio UI: {url}")
    if args.open:
        webbrowser.open(url)
    uvicorn.run(create_app(Settings.from_env(args.env_file), allowed_hosts=allowed, store_dir=DATA_DIR,
                           manual_answers_file=MANUAL_ANSWERS_FILE, replies_file=REPLIES_FILE,
                           ai_answers_file=AI_ANSWERS_FILE, youtube_account_file=YOUTUBE_ACCOUNT_FILE),
                host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
