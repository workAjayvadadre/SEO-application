"""Connect a YouTube channel from the UI: state-checked OAuth callback, saved token, live capability."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx

from market_scraper import Settings

SETTINGS = replace(Settings(), youtube_oauth_client_id="cid.apps.googleusercontent.com",
                   youtube_oauth_client_secret="secret")
ID_TOKEN = "h." + base64.urlsafe_b64encode(json.dumps({"email": "mahesh@example.com"}).encode()).decode().rstrip("=") + ".s"


def google(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "oauth2.googleapis.com"
    form = parse_qs(request.content.decode())
    assert form["code"] == ["good-code"] and form["client_secret"] == ["secret"]
    assert "code_verifier" not in form, "Web client flow: no PKCE"
    return httpx.Response(200, json={"refresh_token": "1//refresh", "access_token": "a", "id_token": ID_TOKEN})


def make(tmp_path, settings=SETTINGS):
    from fastapi.testclient import TestClient
    from server import create_app

    app = create_app(settings, allowed_hosts=["testserver"], youtube_account_file=tmp_path / "yt.json",
                     google_transport=httpx.MockTransport(google))
    return TestClient(app, follow_redirects=False)


def test_connect_from_the_ui_saves_the_channel_and_enables_posting(tmp_path):
    client = make(tmp_path)
    assert client.get("/api/youtube/account").json() == {
        "connected": False, "email": None, "connected_at": None, "source": "app", "client_configured": True}

    start = client.get("/api/youtube/connect")
    assert start.status_code == 303
    google_url = urlsplit(start.headers["location"])
    q = parse_qs(google_url.query)
    assert google_url.netloc == "accounts.google.com"
    assert q["redirect_uri"] == ["http://testserver/api/youtube/callback"]
    assert "youtube.force-ssl" in q["scope"][0] and "code_challenge" not in q

    assert client.get("/api/youtube/callback", params={"code": "good-code", "state": "forged"}).headers[
        "location"] == "/?youtube=expired#replies", "unknown state is refused"
    done = client.get("/api/youtube/callback", params={"code": "good-code", "state": q["state"][0]})
    assert done.headers["location"] == "http://testserver/?youtube=connected#replies"
    assert client.get("/api/youtube/callback", params={"code": "good-code", "state": q["state"][0]}).headers[
        "location"] == "/?youtube=expired#replies", "a state works once"

    status = client.get("/api/youtube/account").json()
    assert status["connected"] is True and status["email"] == "mahesh@example.com"
    assert "1//refresh" not in json.dumps(status), "the token never leaves the server"
    assert client.get("/api/platforms").json()["reply_posting"]["youtube"]["auto"] is True
    assert json.loads((tmp_path / "yt.json").read_text())["refresh_token"] == "1//refresh"

    assert make(tmp_path).get("/api/youtube/account").json()["connected"] is True, "survives a restart"
    assert client.delete("/api/youtube/account").status_code == 204
    assert client.get("/api/youtube/account").json()["connected"] is False
    assert client.get("/api/platforms").json()["reply_posting"]["youtube"]["auto"] is False


def test_cancelled_sign_in_and_missing_client(tmp_path):
    client = make(tmp_path)
    state = parse_qs(urlsplit(client.get("/api/youtube/connect").headers["location"]).query)["state"][0]
    assert client.get("/api/youtube/callback", params={"error": "access_denied", "state": state}).headers[
        "location"].endswith("?youtube=cancelled#replies")
    unconfigured = make(tmp_path, replace(Settings()))
    assert unconfigured.get("/api/youtube/connect").headers["location"] == "/?youtube=no-client#replies"


def test_loopback_callback_is_reused_and_completes_the_sign_in():
    import socket
    import time as _time

    from market_scraper.youtube_account import start_loopback

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    seen = []
    redirect = f"http://localhost:{port}/api/youtube-planner/oauth/callback"
    start_loopback(redirect, lambda q: seen.append(q) or "http://127.0.0.1:8000/?youtube=connected#replies")
    start_loopback(redirect, lambda q: "unused")  # second click: no "port busy"
    _time.sleep(0.2)
    r = httpx.get(f"http://127.0.0.1:{port}/api/youtube-planner/oauth/callback?code=c&state=s", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].endswith("?youtube=connected#replies")
    assert seen == [{"code": "c", "state": "s"}]
