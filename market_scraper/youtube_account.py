"""Connect a YouTube channel from the web UI (Reply queue > "Connect YouTube account").

Google OAuth "authorization code" flow for a Web client:
  /api/youtube/connect  -> Google sign-in (state-protected) -> callback -> refresh token saved in
  data/youtube_account.json (git-ignored) and used immediately; no restart, nothing printed.

The callback address must be registered on the Google OAuth client. YOUTUBE_OAUTH_REDIRECT_URL says
which one to use. When it points to another port on this machine (e.g. the marketing app's
http://localhost:3010/...), a one-shot listener answers there for the duration of the sign-in, so a
client registered for another local app still works. On a server, register
https://<your-domain>/api/youtube/callback and set YOUTUBE_OAUTH_REDIRECT_URL to it.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .config import Settings
from .utils import utc_now_iso

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/youtube.force-ssl openid email"
STATE_TTL = 600  # seconds a sign-in may take


class YouTubeAuthError(RuntimeError):
    """User-facing failure while connecting."""


def client_configured(settings: Settings) -> bool:
    return bool(settings.youtube_oauth_client_id and settings.youtube_oauth_client_secret)


def auth_url(settings: Settings, redirect: str, state: str) -> str:
    # No PKCE: a Web client authenticates with its secret, and Google's channel (Brand Account) picker
    # answers "400 malformed" to a Web client request that carries a code_challenge.
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.youtube_oauth_client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
        "state": state})


def _email_from_id_token(id_token: str | None) -> str | None:
    """The id_token comes straight from Google's token endpoint over TLS, so its payload is read as is."""
    try:
        payload = id_token.split(".")[1]  # type: ignore[union-attr]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email")
    except (AttributeError, IndexError, ValueError):
        return None


def exchange(settings: Settings, code: str, redirect: str, *,
             transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    try:
        with httpx.Client(transport=transport, timeout=30) as client:
            r = client.post(TOKEN_URL, data={
                "client_id": settings.youtube_oauth_client_id, "client_secret": settings.youtube_oauth_client_secret,
                "code": code, "grant_type": "authorization_code", "redirect_uri": redirect})
    except httpx.HTTPError as exc:
        raise YouTubeAuthError(f"Couldn't reach Google to finish sign-in ({type(exc).__name__}).") from exc
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    token = body.get("refresh_token")
    if r.status_code != 200 or not token:
        reason = body.get("error_description") or body.get("error") or f"HTTP {r.status_code}"
        if r.status_code == 200:
            reason = ("Google returned no refresh token. Remove the app at myaccount.google.com/permissions "
                      "and connect again.")
        raise YouTubeAuthError(f"Google refused the sign-in: {reason}")
    return {"refresh_token": token, "email": _email_from_id_token(body.get("id_token"))}


class YouTubeAccount:
    """The connected channel's refresh token, kept in a git-ignored JSON file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        if path:
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                self._data = {}

    @property
    def refresh_token(self) -> str | None:
        return self._data.get("refresh_token")

    def status(self) -> dict[str, Any]:
        return {"connected": bool(self.refresh_token), "email": self._data.get("email"),
                "connected_at": self._data.get("connected_at")}

    def save(self, refresh_token: str, email: str | None) -> None:
        self._data = {"refresh_token": refresh_token, "email": email, "connected_at": utc_now_iso()}
        self._write()

    def clear(self) -> None:
        self._data = {}
        self._write()

    def _write(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data), encoding="utf-8")
        os.replace(tmp, self.path)


class PendingSignIns:
    """state -> where to send the browser afterwards. One use each, expires after STATE_TTL."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, str, str]] = {}

    def new(self, redirect: str, return_to: str) -> str:
        state = secrets.token_urlsafe(24)
        with self._lock:
            now = time.monotonic()
            self._items = {k: v for k, v in self._items.items() if now - v[0] < STATE_TTL}
            self._items[state] = (now, redirect, return_to)
        return state

    def take(self, state: str) -> tuple[str, str] | None:
        with self._lock:
            item = self._items.pop(state or "", None)
        if not item or time.monotonic() - item[0] > STATE_TTL:
            return None
        return item[1], item[2]


_LISTENERS: dict[int, threading.Thread] = {}
_LISTENERS_LOCK = threading.Lock()


def start_loopback(redirect: str, finish: Callable[[dict[str, str]], str]) -> None:
    """Answer the registered callback on another local port for one sign-in (or until STATE_TTL).

    `finish(query)` completes the sign-in and returns where to send the browser next. A second click on
    "Connect" while a listener is already waiting reuses it (states are checked by `finish`).
    """
    parts = urlsplit(redirect)
    port = parts.port or 80
    with _LISTENERS_LOCK:
        running = _LISTENERS.get(port)
        if running and running.is_alive():
            return
        _start_listener(parts, port, finish)


def _start_listener(parts: Any, port: int, finish: Callable[[dict[str, str]], str]) -> None:

    class Handler(http.server.BaseHTTPRequestHandler):
        done = False

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            url = urlsplit(self.path)
            if url.path != parts.path:
                self.send_response(404)
                self.end_headers()
                return
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            location = finish(query)
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()
            type(self).done = True

        def log_message(self, *args: Any) -> None:
            """Silence per-request logging: the query string carries the one-time code."""

    try:
        server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise YouTubeAuthError(f"Port {port} is busy (is another app using it?). Stop it and try again.") from exc
    server.timeout = 1

    def serve() -> None:
        deadline = time.monotonic() + STATE_TTL
        try:
            while not Handler.done and time.monotonic() < deadline:
                server.handle_request()
        finally:
            server.server_close()

    thread = threading.Thread(target=serve, name="youtube-oauth-callback", daemon=True)
    _LISTENERS[port] = thread
    thread.start()
