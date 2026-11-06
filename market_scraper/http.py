"""Resilient async HTTP layer: retries with jittered exponential backoff, Retry-After support,
rotating user agents, proxy rotation with cool-down, and a per-host rate limiter."""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import Settings

log = logging.getLogger(__name__)

USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.6; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
)

API_USER_AGENT = "market-scraper/1.0 (+python-httpx)"

RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524})
# 403 usually means "blocked" on scraped pages (worth retrying through another proxy/UA),
# but means "forbidden / quota exhausted" on official APIs (never worth retrying).
BLOCKED_STATUSES = frozenset({403})


class FetchError(Exception):
    def __init__(self, message: str, *, status: int | None = None, url: str | None = None,
                 body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body


def random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def browser_headers(user_agent: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    }


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """'Full jitter' exponential backoff (AWS architecture blog): uniform(0, min(cap, base*2^n))."""
    return random.uniform(0, min(cap, base * (2 ** attempt)))


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


class ProxyPool:
    """Round-robin proxy rotation; proxies that fail are benched for a cool-down period."""

    def __init__(self, proxies: tuple[str, ...] | list[str], cooldown: float = 60.0) -> None:
        self._proxies = list(proxies)
        self._cycle = itertools.cycle(self._proxies) if self._proxies else None
        self._benched_until: dict[str, float] = {}
        self._cooldown = cooldown

    def __bool__(self) -> bool:
        return bool(self._proxies)

    def next(self) -> str | None:
        if self._cycle is None:
            return None
        now = time.monotonic()
        for _ in range(len(self._proxies)):
            proxy = next(self._cycle)
            if self._benched_until.get(proxy, 0.0) <= now:
                return proxy
        return next(self._cycle)  # everything is benched: least-bad option is to keep rotating

    def penalize(self, proxy: str | None) -> None:
        if proxy:
            self._benched_until[proxy] = time.monotonic() + self._cooldown


class HostRateLimiter:
    """Spaces requests to the same host at least 1/rps seconds apart, without blocking other hosts."""

    def __init__(self, requests_per_second: float) -> None:
        self._interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._next_slot: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def wait(self, host: str) -> None:
        if not self._interval:
            return
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(host, 0.0))
            self._next_slot[host] = slot + self._interval
        if (delay := slot - now) > 0:
            await asyncio.sleep(delay)


class HttpClient:
    """Shared async client. One pooled `httpx.AsyncClient` per proxy so connections are reused."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.proxies = ProxyPool(settings.proxies)
        self._transport = transport
        self._clients: dict[str | None, httpx.AsyncClient] = {}
        self._limiter = HostRateLimiter(settings.requests_per_second_per_host)
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        await asyncio.gather(*(c.aclose() for c in clients), return_exceptions=True)

    def _client(self, proxy: str | None) -> httpx.AsyncClient:
        client = self._clients.get(proxy)
        if client is None:
            kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(self.settings.http_timeout),
                "follow_redirects": True,
                "limits": httpx.Limits(max_connections=self.settings.max_concurrency * 2,
                                       max_keepalive_connections=self.settings.max_concurrency),
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            elif proxy:
                kwargs["proxy"] = proxy
            client = self._clients[proxy] = httpx.AsyncClient(**kwargs)
        return client

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Mapping[str, Any] | None = None,
        auth: tuple[str, str] | None = None,
        use_proxy: bool = True,
        browser_like: bool = False,
    ) -> httpx.Response:
        """Send a request, retrying transient failures. Raises FetchError when retries run out.

        `use_proxy=False` for official APIs (they authenticate by key, proxies only add latency).
        `browser_like=True` for HTML pages: full browser headers, and 403 is treated as retryable.
        """
        host = urlsplit(url).hostname or ""
        # A 403 block is only worth retrying if the next attempt comes from a different IP.
        retryable = RETRY_STATUSES | (BLOCKED_STATUSES if browser_like and self.proxies else frozenset())
        retries = max(self.settings.max_retries, 0)
        last_error: FetchError | None = None

        for attempt in range(retries + 1):
            proxy = self.proxies.next() if use_proxy else None
            # Official APIs get an honest client UA; a fake browser UA there gains nothing and some
            # corporate proxies (e.g. Zscaler) demand browser sign-in for anything claiming to be a browser.
            request_headers = (browser_headers(random_user_agent()) if browser_like
                               else {"User-Agent": API_USER_AGENT})
            if headers:
                request_headers.update(headers)

            await self._limiter.wait(host)
            server_delay: float | None = None
            try:
                async with self._semaphore:
                    response = await self._client(proxy).request(
                        method, url, params=params, headers=request_headers, json=json, data=data, auth=auth
                    )
            except httpx.TransportError as exc:  # DNS, connect, read timeouts, proxy failures
                self.proxies.penalize(proxy)
                last_error = FetchError(f"{type(exc).__name__}: {exc}", url=url)
            else:
                if response.status_code < 400:
                    return response
                message = f"HTTP {response.status_code} from {host}"
                if "Zscaler" in response.text[:20000]:
                    message += " (blocked by your company's network filter - ask IT to allow this site)"
                error = FetchError(message, status=response.status_code, url=url, body=response.text[:500])
                if response.status_code not in retryable:
                    raise error
                if response.status_code in (403, 429):
                    self.proxies.penalize(proxy)
                last_error = error
                server_delay = retry_after_seconds(response)

            if attempt >= retries:
                break
            delay = (min(server_delay, self.settings.backoff_max) if server_delay is not None
                     else backoff_delay(attempt, self.settings.backoff_base, self.settings.backoff_max))
            log.warning("Retry %d/%d for %s %s in %.1fs (%s)", attempt + 1, retries, method, url, delay, last_error)
            await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return await self.json("GET", url, **kwargs)

    async def json(self, method: str, url: str, **kwargs: Any) -> Any:
        response = await self.request(method, url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"Invalid JSON from {url}", status=response.status_code, url=url) from exc

    async def get_text(self, url: str, **kwargs: Any) -> str:
        kwargs.setdefault("browser_like", True)
        return (await self.request("GET", url, **kwargs)).text
