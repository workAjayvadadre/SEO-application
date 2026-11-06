"""One-time YouTube sign-in for the reply queue: prints a refresh token for YOUTUBE_OAUTH_REFRESH_TOKEN.

Prerequisites (Google Cloud console, same project as YOUTUBE_API_KEY):
  1. APIs & Services > OAuth consent screen: set it up and add the channel owner's Google account as a test user.
  2. Credentials > Create credentials > OAuth client ID > "Desktop app".
  3. Put its client id and secret in .env as YOUTUBE_OAUTH_CLIENT_ID / YOUTUBE_OAUTH_CLIENT_SECRET.

Run:  python scripts/youtube_oauth.py
A browser opens; sign in with the account that owns (or manages) the brand's YouTube channel and allow
"Manage your YouTube account" (needed to post comments). The token is printed once: paste it into .env and
keep it secret. Revoke it any time at https://myaccount.google.com/permissions
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import os
import secrets
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from market_scraper import Settings, use_system_certificates  # noqa: E402

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"


def main() -> int:
    use_system_certificates()
    settings = Settings.from_env(Path(__file__).resolve().parent.parent / ".env")
    if not (settings.youtube_oauth_client_id and settings.youtube_oauth_client_secret):
        print("Set YOUTUBE_OAUTH_CLIENT_ID and YOUTUBE_OAUTH_CLIENT_SECRET in .env first (see this file's header).")
        return 1

    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            query = parse_qs(urlsplit(self.path).query)
            if query.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                return
            result["code"] = query.get("code", [""])[0]
            result["error"] = query.get("error", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and go back to the terminal.")

        def log_message(self, *args) -> None:
            pass

    # A "Web application" OAuth client only accepts its registered redirect URL: listen exactly there.
    # A "Desktop app" client accepts any loopback port, so a free one is picked.
    registered = os.getenv("YOUTUBE_OAUTH_REDIRECT_URL", "").strip()
    if registered:
        parts = urlsplit(registered)
        if parts.hostname not in ("localhost", "127.0.0.1") or parts.scheme != "http":
            print("YOUTUBE_OAUTH_REDIRECT_URL must be an http://localhost:<port>/... address to use this script.")
            return 1
        try:
            server = http.server.HTTPServer(("127.0.0.1", parts.port or 80), Handler)
        except OSError:
            print(f"Port {parts.port} is in use (is the marketing app running?). Stop it and run this again.")
            return 1
        redirect = registered
    else:
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        redirect = f"http://127.0.0.1:{server.server_port}/"
    params = {"client_id": settings.youtube_oauth_client_id, "redirect_uri": redirect, "response_type": "code",
              "scope": SCOPE, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
              "state": state}
    # PKCE only for Desktop clients. A Web client authenticates with its secret, and Google's channel
    # (Brand Account) picker answers "400 malformed" to a Web client request that carries a code_challenge.
    use_pkce = not registered
    if use_pkce:
        params.update(code_challenge=challenge, code_challenge_method="S256")
    url = AUTH_URL + "?" + urlencode(params)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    print("Opening the Google sign-in page. If it doesn't open, paste this link into your browser:\n\n" + url + "\n")
    webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()

    if not result.get("code"):
        print(f"Sign-in did not complete ({result.get('error') or 'timed out after 5 minutes'}).")
        return 1
    exchange = {"client_id": settings.youtube_oauth_client_id, "client_secret": settings.youtube_oauth_client_secret,
                "code": result["code"], "grant_type": "authorization_code", "redirect_uri": redirect}
    if use_pkce:
        exchange["code_verifier"] = verifier
    response = httpx.post(TOKEN_URL, data=exchange, timeout=30)
    token = response.json().get("refresh_token") if response.status_code == 200 else None
    if not token:
        print(f"Google refused the exchange (HTTP {response.status_code}). Check the client id/secret and try again.")
        return 1
    # Saved straight into .env (never printed): the token lets anyone comment as the channel.
    env_path = Path(__file__).resolve().parent.parent / ".env"
    lines = [line for line in env_path.read_text(encoding="utf-8").splitlines()
             if line.split("=", 1)[0].strip() != "YOUTUBE_OAUTH_REFRESH_TOKEN"]
    env_path.write_text("\n".join([*lines, f"YOUTUBE_OAUTH_REFRESH_TOKEN={token}"]) + "\n", encoding="utf-8")
    name = "(channel name unavailable: www.googleapis.com not reachable from this network)"
    try:
        channel = httpx.get("https://www.googleapis.com/youtube/v3/channels", timeout=15,
                            params={"part": "snippet", "mine": "true"},
                            headers={"Authorization": f"Bearer {response.json().get('access_token', '')}"})
        items = channel.json().get("items", []) if channel.status_code == 200 else []
        if items:
            name = items[0]["snippet"]["title"]
    except httpx.HTTPError:
        pass
    print(f"Saved YOUTUBE_OAUTH_REFRESH_TOKEN to .env for channel: {name}. Restart the server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
