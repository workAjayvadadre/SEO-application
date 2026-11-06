"""HTML fetching for pages without APIs.

`PageFetcher` uses plain HTTP by default and switches to a headless Chromium (Playwright) when
PLAYWRIGHT_ENABLED=true. Playwright is imported lazily so it stays an optional dependency.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any
from urllib.parse import urlsplit

from .config import Settings
from .http import (BLOCKED_STATUSES, RETRY_STATUSES, FetchError, HttpClient, ProxyPool,
                   backoff_delay, random_user_agent)

log = logging.getLogger(__name__)

_VIEWPORTS = ({"width": 1366, "height": 768}, {"width": 1440, "height": 900},
              {"width": 1536, "height": 864}, {"width": 1920, "height": 1080})
_SKIPPED_RESOURCES = frozenset({"image", "media", "font", "stylesheet"})
# Hides the most obvious automation flag. Keeps pages rendering as they would for a normal visitor.
_INIT_SCRIPT = "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"


def playwright_proxy(proxy_url: str) -> dict[str, str]:
    """Convert `http://user:pass@host:port` to Playwright's proxy dict."""
    parts = urlsplit(proxy_url)
    server = f"{parts.scheme or 'http'}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    proxy = {"server": server}
    if parts.username:
        proxy["username"] = parts.username
    if parts.password:
        proxy["password"] = parts.password
    return proxy


class _BrowserFetcher:
    def __init__(self, settings: Settings, proxies: ProxyPool) -> None:
        self.settings = settings
        self.proxies = proxies
        self._playwright: Any = None
        self._browser: Any = None
        self._start_lock = asyncio.Lock()
        self._pages = asyncio.Semaphore(max(settings.playwright_max_pages, 1))

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._browser is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise FetchError("PLAYWRIGHT_ENABLED=true but playwright isn't installed. "
                                 "Run: pip install playwright && playwright install chromium") from exc
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self.settings.playwright_headless)

    @staticmethod
    async def _route(route: Any) -> None:
        if route.request.resource_type in _SKIPPED_RESOURCES:
            await route.abort()
        else:
            await route.continue_()

    async def _attempt(self, url: str) -> str:
        proxy = self.proxies.next()
        context = await self._browser.new_context(
            user_agent=random_user_agent(),
            viewport=random.choice(_VIEWPORTS),
            locale="en-US",
            proxy=playwright_proxy(proxy) if proxy else None,
        )
        try:
            await context.add_init_script(_INIT_SCRIPT)
            await context.route("**/*", self._route)
            page = await context.new_page()
            response = await page.goto(url, wait_until="domcontentloaded",
                                       timeout=int(self.settings.http_timeout * 1000) * 2)
            status = response.status if response else 0
            if status >= 400:
                if status in RETRY_STATUSES | BLOCKED_STATUSES:
                    self.proxies.penalize(proxy)
                raise FetchError(f"HTTP {status} (browser)", status=status, url=url)
            await page.wait_for_timeout(random.randint(600, 1500))  # let client-side hydration finish
            return await page.content()
        except FetchError:
            raise
        except Exception as exc:  # Playwright timeouts / navigation errors
            self.proxies.penalize(proxy)
            raise FetchError(f"{type(exc).__name__}: {exc}", url=url) from exc
        finally:
            await context.close()

    async def fetch_html(self, url: str) -> str:
        await self._ensure_started()
        retries = max(self.settings.max_retries, 0)
        async with self._pages:
            for attempt in range(retries + 1):
                try:
                    return await self._attempt(url)
                except FetchError as exc:
                    retryable = exc.status is None or exc.status in RETRY_STATUSES | BLOCKED_STATUSES
                    if not retryable or attempt >= retries:
                        raise
                    delay = backoff_delay(attempt, self.settings.backoff_base, self.settings.backoff_max)
                    log.warning("Browser retry %d/%d for %s in %.1fs (%s)", attempt + 1, retries, url, delay, exc)
                    await asyncio.sleep(delay)
        raise FetchError("unreachable", url=url)

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._browser = self._playwright = None


class PageFetcher:
    """Fetches rendered HTML via Playwright when enabled, otherwise via the HTTP client."""

    def __init__(self, settings: Settings, http: HttpClient) -> None:
        self._http = http
        self._browser = _BrowserFetcher(settings, http.proxies) if settings.playwright_enabled else None

    async def __aenter__(self) -> PageFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._browser is not None:
            await self._browser.aclose()

    async def fetch_html(self, url: str) -> str:
        if self._browser is not None:
            return await self._browser.fetch_html(url)
        return await self._http.get_text(url)
