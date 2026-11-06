"""Environment-driven configuration. Every knob lives in `.env` (see `.env.example`)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _bool(name: str, default: bool) -> bool:
    value = _get(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = _get(name)
    return int(value) if value is not None else default


def _float(name: str, default: float) -> float:
    value = _get(name)
    return float(value) if value is not None else default


def _csv(name: str) -> tuple[str, ...]:
    value = _get(name)
    return tuple(part.strip() for part in value.split(",") if part.strip()) if value else ()


def _load_proxies() -> tuple[str, ...]:
    proxies = list(_csv("PROXY_URLS"))
    proxy_file = _get("PROXY_FILE")
    if proxy_file and Path(proxy_file).is_file():
        for line in Path(proxy_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                proxies.append(line)
    return tuple(dict.fromkeys(proxies))  # de-duplicate, keep order


@dataclass(frozen=True, slots=True)
class Settings:
    # --- SERP provider (Google, plus discovery for Quora / Meta) ---
    serper_api_key: str | None = None
    serpapi_api_key: str | None = None
    serp_provider_override: str | None = None
    search_country: str = "us"
    search_language: str = "en"

    # --- Reddit (official OAuth API) ---
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "python:market-scraper:1.0 (by /u/your_reddit_username)"
    reddit_sort: str = "relevance"
    reddit_time_filter: str = "month"
    reddit_comment_posts: int = 5

    # --- YouTube Data API v3 ---
    youtube_api_key: str | None = None
    youtube_order: str = "relevance"
    youtube_comment_videos: int = 5

    # --- Meta Graph API (Instagram / Facebook) ---
    meta_access_token: str | None = None
    meta_app_secret: str | None = None
    meta_graph_version: str = "v21.0"
    ig_business_account_id: str | None = None
    ig_hashtag_edge: str = "top_media"
    ig_discovery_usernames: tuple[str, ...] = ()
    fb_page_ids: tuple[str, ...] = ()
    meta_serp_discovery: bool = True

    # --- Quora ---
    quora_fetch_answers: bool = True
    quora_max_pages: int = 5

    # --- Content writer (Claude API) ---
    anthropic_api_key: str | None = None
    content_model: str = "claude-opus-5-5"
    content_effort: str = "medium"
    brand_name: str | None = "Indira IVF"  # institution's legal/brand name, shown on every generated piece (NMC 8.3 i)

    # --- Reply queue: posting approved replies (each needs a human approval first) ---
    reddit_username: str | None = None  # brand account; script-app password grant (2FA must be off)
    reddit_password: str | None = None
    youtube_oauth_client_id: str | None = None  # Google OAuth "Desktop app" client
    youtube_oauth_client_secret: str | None = None
    youtube_oauth_refresh_token: str | None = None  # from scripts/youtube_oauth.py
    meta_page_access_token: str | None = None  # Page token with pages_manage_engagement / instagram_manage_comments
    reply_daily_limit: int = 10  # max auto-posted replies per platform per day (spam guard)
    own_brand_terms: tuple[str, ...] = ("indira",)  # searches naming us: label "own brand"
    competitor_terms: tuple[str, ...] = ()  # extra competitor names on top of the built-in list

    # --- Headless browser (Playwright) ---
    playwright_enabled: bool = False
    playwright_headless: bool = True
    playwright_max_pages: int = 2

    # --- Resilience / performance ---
    proxies: tuple[str, ...] = ()
    http_timeout: float = 20.0
    max_retries: int = 4
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    max_concurrency: int = 10
    requests_per_second_per_host: float = 2.0
    source_timeout: float = 120.0
    keyword_rps: float = 8.0  # Google Suggest pacing for the keyword engine (~50 calls per keyword)

    # --- Defaults for the CLI ---
    default_limit: int = 10
    default_comments: int = 10

    @property
    def serp_provider(self) -> str | None:
        """Explicit SERP_PROVIDER wins; otherwise pick whichever key is present."""
        if self.serp_provider_override:
            return self.serp_provider_override.lower()
        if self.serper_api_key:
            return "serper"
        if self.serpapi_api_key:
            return "serpapi"
        return None

    @classmethod
    def from_env(cls, env_file: str | os.PathLike[str] | None = ".env") -> Settings:
        if env_file and Path(env_file).is_file():
            load_dotenv(env_file, override=False)
        return cls(
            serper_api_key=_get("SERPER_API_KEY"),
            serpapi_api_key=_get("SERPAPI_API_KEY"),
            serp_provider_override=_get("SERP_PROVIDER"),
            search_country=_get("SEARCH_COUNTRY", "us"),
            search_language=_get("SEARCH_LANGUAGE", "en"),
            reddit_client_id=_get("REDDIT_CLIENT_ID"),
            reddit_client_secret=_get("REDDIT_CLIENT_SECRET"),
            reddit_user_agent=_get("REDDIT_USER_AGENT", cls.reddit_user_agent),
            reddit_sort=_get("REDDIT_SORT", "relevance"),
            reddit_time_filter=_get("REDDIT_TIME_FILTER", "month"),
            reddit_comment_posts=_int("REDDIT_COMMENT_POSTS", 5),
            youtube_api_key=_get("YOUTUBE_API_KEY"),
            youtube_order=_get("YOUTUBE_ORDER", "relevance"),
            youtube_comment_videos=_int("YOUTUBE_COMMENT_VIDEOS", 5),
            meta_access_token=_get("META_ACCESS_TOKEN"),
            meta_app_secret=_get("META_APP_SECRET"),
            meta_graph_version=_get("META_GRAPH_VERSION", "v21.0"),
            ig_business_account_id=_get("IG_BUSINESS_ACCOUNT_ID"),
            ig_hashtag_edge=_get("IG_HASHTAG_EDGE", "top_media"),
            ig_discovery_usernames=_csv("IG_DISCOVERY_USERNAMES"),
            fb_page_ids=_csv("FB_PAGE_IDS"),
            meta_serp_discovery=_bool("META_SERP_DISCOVERY", True),
            quora_fetch_answers=_bool("QUORA_FETCH_ANSWERS", True),
            quora_max_pages=_int("QUORA_MAX_PAGES", 5),
            anthropic_api_key=_get("ANTHROPIC_API_KEY"),
            content_model=_get("CONTENT_MODEL", "claude-opus-5-5"),
            content_effort=_get("CONTENT_EFFORT", "medium"),
            brand_name=_get("BRAND_NAME", "Indira IVF"),
            reddit_username=_get("REDDIT_USERNAME"),
            reddit_password=_get("REDDIT_PASSWORD"),
            youtube_oauth_client_id=_get("YOUTUBE_OAUTH_CLIENT_ID"),
            youtube_oauth_client_secret=_get("YOUTUBE_OAUTH_CLIENT_SECRET"),
            youtube_oauth_refresh_token=_get("YOUTUBE_OAUTH_REFRESH_TOKEN"),
            meta_page_access_token=_get("META_PAGE_ACCESS_TOKEN"),
            reply_daily_limit=_int("REPLY_DAILY_LIMIT", 10),
            own_brand_terms=_csv("OWN_BRAND_TERMS") or ("indira",),
            competitor_terms=_csv("COMPETITOR_TERMS"),
            playwright_enabled=_bool("PLAYWRIGHT_ENABLED", False),
            playwright_headless=_bool("PLAYWRIGHT_HEADLESS", True),
            playwright_max_pages=_int("PLAYWRIGHT_MAX_PAGES", 2),
            proxies=_load_proxies(),
            http_timeout=_float("HTTP_TIMEOUT", 20.0),
            max_retries=_int("MAX_RETRIES", 4),
            backoff_base=_float("BACKOFF_BASE", 1.0),
            backoff_max=_float("BACKOFF_MAX", 30.0),
            max_concurrency=_int("MAX_CONCURRENCY", 10),
            requests_per_second_per_host=_float("REQUESTS_PER_SECOND_PER_HOST", 2.0),
            source_timeout=_float("SOURCE_TIMEOUT", 120.0),
            keyword_rps=_float("KEYWORD_RPS", 8.0),
            default_limit=_int("DEFAULT_LIMIT", 10),
            default_comments=_int("DEFAULT_COMMENTS", 10),
        )
