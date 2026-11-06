"""Reply queue (ORM): targets, approval rules, posting and the same-origin guard. No real network calls."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from market_scraper import Settings
from market_scraper.publishers import target_for

BRAND = "Indira IVF"
REDDIT = "https://www.reddit.com/r/IVF/comments/abc12/is_ivf_painful/"
POSTABLE = replace(Settings(), brand_name=BRAND, reddit_client_id="id", reddit_client_secret="s",
                   reddit_username="brand", reddit_password="pw", reply_daily_limit=1)
GOOD = f"Most people feel mild cramps; a specialist can explain options. - {BRAND} team (official account)"


def test_targets_cover_only_what_platform_apis_allow():
    assert target_for("reddit", REDDIT).kind == "t3"
    comment = target_for("reddit", "https://old.reddit.com/r/IVF/comments/abc12/x/def34/")
    assert (comment.kind, comment.ident) == ("t1", "def34")
    video = target_for("youtube", "https://www.youtube.com/watch?v=YWrgyHn0VSY")
    assert (video.kind, video.ident) == ("video", "YWrgyHn0VSY")
    reply = target_for("youtube", "https://www.youtube.com/watch?v=YWrgyHn0VSY&lc=UgzABCDEF123")
    assert (reply.kind, reply.ident, reply.parent) == ("comment", "UgzABCDEF123", "YWrgyHn0VSY")
    assert target_for("facebook", "https://www.facebook.com/x", graph_id="123_456").ident == "123_456"
    assert target_for("instagram", "https://www.instagram.com/p/x/") is None  # no media id, no API target
    assert target_for("quora", "https://www.quora.com/Is-IVF-painful") is None


@pytest.fixture
def make_client():
    from fastapi.testclient import TestClient
    from server import create_app

    def build(settings: Settings, handler=None):
        posted: list[httpx.Request] = []

        def default(request: httpx.Request) -> httpx.Response:
            posted.append(request)
            if request.url.path == "/api/v1/access_token":
                return httpx.Response(200, json={"access_token": "tok"})
            if request.url.path == "/api/comment":
                return httpx.Response(200, json={"json": {"errors": [], "data": {"things": [
                    {"data": {"permalink": "/r/IVF/comments/abc12/x/new1/"}}]}}})
            return httpx.Response(404)

        app = create_app(settings, transport=httpx.MockTransport(handler or default), allowed_hosts=["testserver"])
        return TestClient(app), posted
    return build


def add(client, url=REDDIT, platform="reddit"):
    r = client.post("/api/replies", json={"question": "Is IVF painful?", "platform": platform,
                                          "source_url": url, "context": ["It hurts a lot"]})
    assert r.status_code == 200, r.text
    return r.json()


def test_reply_must_be_approved_compliant_and_disclosed_before_posting(make_client):
    client, posted = make_client(POSTABLE)
    item = add(client)
    assert add(client)["id"] == item["id"], "one queue entry per question"
    rid = item["id"]

    assert client.post(f"/api/replies/{rid}/post", json={"by": "Asha", "confirm": True}).status_code == 409
    client.put(f"/api/replies/{rid}", json={"reply": "We are the best IVF clinic, 100% painless. - Indira IVF team"})
    refused = client.post(f"/api/replies/{rid}/approve", json={"approver": "Asha"})
    assert refused.status_code == 409 and "NMC" in refused.json()["detail"]
    client.put(f"/api/replies/{rid}", json={"reply": "Mild cramps are common."})
    assert "must name" in client.post(f"/api/replies/{rid}/approve", json={"approver": "Asha"}).json()["detail"]

    client.put(f"/api/replies/{rid}", json={"reply": GOOD})
    approved = client.post(f"/api/replies/{rid}/approve", json={"approver": "Asha"}).json()
    assert approved["status"] == "approved" and approved["auto_post"] is True
    edited = client.put(f"/api/replies/{rid}", json={"reply": GOOD + " "}).json()
    assert edited["status"] == "draft" and edited["approved_by"] is None, "editing withdraws approval"
    client.post(f"/api/replies/{rid}/approve", json={"approver": "Asha"})

    assert client.post(f"/api/replies/{rid}/post", json={"by": "Asha"}).status_code == 422, "confirm is required"
    done = client.post(f"/api/replies/{rid}/post", json={"by": "Asha", "confirm": True}).json()
    assert done["status"] == "posted" and done["posted_url"].endswith("/new1/")
    sent = next(r for r in posted if r.url.path == "/api/comment")
    assert b"thing_id=t3_abc12" in sent.content
    assert [e["event"] for e in done["log"]][-1] == "posted via reddit API"

    assert client.post(f"/api/replies/{rid}/post", json={"by": "Asha", "confirm": True}).status_code == 409
    assert client.delete(f"/api/replies/{rid}").status_code == 409, "posted replies stay in the log"

    second = add(client, url="https://www.reddit.com/r/IVF/comments/zzz99/other/")
    client.put(f"/api/replies/{second['id']}", json={"reply": GOOD})
    client.post(f"/api/replies/{second['id']}/approve", json={"approver": "Asha"})
    limited = client.post(f"/api/replies/{second['id']}/post", json={"by": "Asha", "confirm": True})
    assert limited.status_code == 409 and "Daily limit" in limited.json()["detail"]


def test_quora_replies_are_posted_by_hand_and_recorded(make_client):
    client, posted = make_client(POSTABLE)
    item = add(client, url="https://www.quora.com/Is-IVF-painful", platform="quora")
    assert item["auto_post"] is False and "Quora" in item["post_route"]
    client.put(f"/api/replies/{item['id']}", json={"reply": GOOD})
    client.post(f"/api/replies/{item['id']}/approve", json={"approver": "Asha"})
    assert client.post(f"/api/replies/{item['id']}/post", json={"by": "Asha", "confirm": True}).status_code == 409
    done = client.post(f"/api/replies/{item['id']}/mark-posted",
                       json={"by": "Asha", "url": "https://www.quora.com/Is-IVF-painful/answer/Indira"}).json()
    assert done["status"] == "posted" and done["post_method"] == "manual"
    assert not posted, "nothing was sent to any platform"


def test_failed_post_is_reported_and_can_be_retried(make_client):
    def refuse(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json={"json": {"errors": [["THREAD_LOCKED", "that thread is locked", "parent"]]}})

    client, _ = make_client(POSTABLE, refuse)
    rid = add(client)["id"]
    client.put(f"/api/replies/{rid}", json={"reply": GOOD})
    client.post(f"/api/replies/{rid}/approve", json={"approver": "Asha"})
    failed = client.post(f"/api/replies/{rid}/post", json={"by": "Asha", "confirm": True}).json()
    assert failed["status"] == "failed" and "THREAD_LOCKED" in failed["error"]
    assert "pw" not in failed["error"] and "tok" not in failed["error"], "credentials never reach messages"


def test_claude_draft_fills_the_reply(make_client, monkeypatch):
    import server

    async def fake_draft(settings, item):
        assert item["context"] == ["It hurts a lot"]
        return {"reply": GOOD, "language": "English", "content_class": "A",
                "compliance": {"verdict": "GREEN", "notes": [], "placeholders": []}}

    monkeypatch.setattr(server, "draft_reply", fake_draft)
    client, _ = make_client(POSTABLE)
    drafted = client.post(f"/api/replies/{add(client)['id']}/draft").json()
    assert drafted["reply"] == GOOD and drafted["compliance"]["verdict"] == "GREEN" and drafted["status"] == "draft"


def test_other_websites_cannot_drive_the_queue(make_client):
    client, _ = make_client(POSTABLE)
    rid = add(client)["id"]
    evil = client.post(f"/api/replies/{rid}/approve", json={"approver": "x"},
                       headers={"Origin": "https://evil.example.com"})
    assert evil.status_code == 403
    same = client.put(f"/api/replies/{rid}", json={"reply": GOOD}, headers={"Origin": "http://testserver"})
    assert same.status_code == 200


def test_behind_nginx_the_public_site_can_write_but_other_sites_cannot():
    from fastapi.testclient import TestClient
    from server import create_app

    client = TestClient(create_app(POSTABLE, allowed_hosts=["127.0.0.1", "localhost", "seo.example.com"]),
                        base_url="https://seo.example.com")
    rid = add(client)["id"]
    ok = client.put(f"/api/replies/{rid}", json={"reply": GOOD}, headers={"Origin": "https://seo.example.com"})
    assert ok.status_code == 200
    evil = client.put(f"/api/replies/{rid}", json={"reply": "x"}, headers={"Origin": "https://evil.example.com"})
    assert evil.status_code == 403

    open_server = TestClient(create_app(POSTABLE, allowed_hosts=["*"]), base_url="http://10.0.0.5:8000")
    rid = add(open_server)["id"]
    assert open_server.put(f"/api/replies/{rid}", json={"reply": GOOD},
                           headers={"Origin": "http://10.0.0.5:8000"}).status_code == 200
    assert open_server.put(f"/api/replies/{rid}", json={"reply": "x"},
                           headers={"Origin": "https://evil.example.com"}).status_code == 403, '"*" trusts no origin'
