"""Post an approved reply through each platform's official API.

Only what the platforms allow programmatically:
- Reddit: comment on a thread or reply to a comment, as the brand account (script app, password grant).
- YouTube: top-level comment on a video, or reply to a comment, as the channel (OAuth refresh token).
- Facebook / Instagram: comment on a post or media item of *your own* Page / account (Page token).
- Quora, Google and everything else: no posting API. Those replies are copied and posted by hand.

Posts are never retried automatically: a timeout after the request left could mean it was published,
and a blind retry would double-post. Credentials only ever travel in request bodies or headers, so
error messages (which include URLs) cannot leak them.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from .config import Settings

REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_API = "https://oauth.reddit.com"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"

_REDDIT_PATH = re.compile(r"/comments/([a-z0-9]+)(?:/[^/]*/([a-z0-9]+))?", re.I)
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_GRAPH_ID = re.compile(r"^[0-9_]{3,64}$")

# Platform limits on comment length (characters).
MAX_LENGTH = {"reddit": 10000, "youtube": 10000, "facebook": 8000, "instagram": 2200}


class PublishError(RuntimeError):
    """User-facing failure: missing setup, unsupported target, or the platform refused the post."""


@dataclass(frozen=True)
class Target:
    platform: str
    kind: str  # reddit: t3/t1 · youtube: video/comment · facebook/instagram: object
    ident: str
    parent: str | None = None  # youtube: the video id of a comment reply

    def describe(self) -> str:
        return {"t3": "comment on the Reddit thread", "t1": "reply to the Reddit comment",
                "video": "comment on the YouTube video", "comment": "reply to the YouTube comment",
                "object": f"comment on the {self.platform.title()} post"}.get(self.kind, self.kind)


def target_for(platform: str, source_url: str, graph_id: str | None = None) -> Target | None:
    """Where an auto-post would go, or None when the platform/link can only be answered by hand."""
    parts = urlsplit(source_url or "")
    host = (parts.hostname or "").lower()
    if platform == "reddit" and host.endswith("reddit.com"):
        m = _REDDIT_PATH.search(parts.path)
        if m:
            return Target("reddit", "t1", m.group(2).lower()) if m.group(2) else Target("reddit", "t3", m.group(1).lower())
    if platform == "youtube" and host.endswith("youtube.com"):
        query = parse_qs(parts.query)
        video = (query.get("v") or [""])[0]
        comment = (query.get("lc") or [""])[0]
        if comment and _YT_ID.match(comment):
            return Target("youtube", "comment", comment, parent=video or None)
        if video and _YT_ID.match(video):
            return Target("youtube", "video", video)
    if platform in ("facebook", "instagram") and graph_id and _GRAPH_ID.match(str(graph_id)):
        return Target(platform, "object", str(graph_id))
    return None


def capabilities(settings: Settings) -> dict[str, dict[str, Any]]:
    """Per platform: can approved replies be auto-posted, and if not, what is missing."""
    meta = bool(settings.meta_page_access_token)
    meta_reason = "" if meta else "Set META_PAGE_ACCESS_TOKEN (Page token) in .env"

    def entry(ok: bool, reason: str) -> dict[str, Any]:
        return {"auto": ok, "reason": "" if ok else reason}

    return {
        "reddit": entry(bool(settings.reddit_client_id and settings.reddit_client_secret
                             and settings.reddit_username and settings.reddit_password),
                        "Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME and REDDIT_PASSWORD in .env"),
        "youtube": entry(bool(settings.youtube_oauth_client_id and settings.youtube_oauth_client_secret
                              and settings.youtube_oauth_refresh_token),
                         "Set YOUTUBE_OAUTH_CLIENT_ID / _SECRET and run scripts/youtube_oauth.py for the refresh token"),
        "facebook": entry(meta, meta_reason),
        "instagram": entry(meta, meta_reason),
        "quora": entry(False, "Quora has no posting API: copy the reply and post it by hand"),
        "google": entry(False, "Google results can't be replied to: answer them with blog / FAQ content"),
    }


def _refused(platform: str, response: httpx.Response) -> PublishError:
    detail = ""
    try:
        body = response.json()
        detail = (body.get("error", {}).get("message") if isinstance(body.get("error"), dict)
                  else body.get("message") or body.get("error") or "")
    except ValueError:
        pass
    return PublishError(f"{platform} refused the post (HTTP {response.status_code}){': ' + str(detail)[:300] if detail else ''}")


async def _reddit(client: httpx.AsyncClient, settings: Settings, target: Target, text: str) -> str | None:
    ua = {"User-Agent": settings.reddit_user_agent}
    token = await client.post(REDDIT_TOKEN_URL, headers=ua,
                              auth=(settings.reddit_client_id or "", settings.reddit_client_secret or ""),
                              data={"grant_type": "password", "username": settings.reddit_username,
                                    "password": settings.reddit_password})
    access = token.json().get("access_token") if token.status_code == 200 else None
    if not access:
        raise PublishError("Reddit login failed: check the app id/secret and the brand account's username/password "
                           "(accounts with 2FA can't use this login).")
    r = await client.post(f"{REDDIT_API}/api/comment", headers={**ua, "Authorization": f"bearer {access}"},
                          data={"thing_id": f"{target.kind}_{target.ident}", "text": text, "api_type": "json"})
    if r.status_code != 200:
        raise _refused("Reddit", r)
    body = r.json().get("json", {})
    if body.get("errors"):
        raise PublishError("Reddit refused the post: " + "; ".join(" ".join(map(str, e)) for e in body["errors"])[:300])
    things = body.get("data", {}).get("things") or [{}]
    permalink = things[0].get("data", {}).get("permalink")
    return f"https://www.reddit.com{permalink}" if permalink else None


async def _youtube(client: httpx.AsyncClient, settings: Settings, target: Target, text: str) -> str | None:
    token = await client.post(GOOGLE_TOKEN_URL, data={
        "client_id": settings.youtube_oauth_client_id, "client_secret": settings.youtube_oauth_client_secret,
        "refresh_token": settings.youtube_oauth_refresh_token, "grant_type": "refresh_token"})
    access = token.json().get("access_token") if token.status_code == 200 else None
    if not access:
        raise PublishError("YouTube sign-in failed: the refresh token is missing, revoked or expired. "
                           "Run scripts/youtube_oauth.py again.")
    auth = {"Authorization": f"Bearer {access}"}
    if target.kind == "comment":
        r = await client.post(f"{YOUTUBE_API}/comments", params={"part": "snippet"}, headers=auth,
                              json={"snippet": {"parentId": target.ident, "textOriginal": text}})
        video = target.parent
    else:
        r = await client.post(f"{YOUTUBE_API}/commentThreads", params={"part": "snippet"}, headers=auth,
                              json={"snippet": {"videoId": target.ident,
                                                "topLevelComment": {"snippet": {"textOriginal": text}}}})
        video = target.ident
    if r.status_code not in (200, 201):
        raise _refused("YouTube", r)
    new_id = r.json().get("id")
    return f"https://www.youtube.com/watch?v={video}&lc={new_id}" if video and new_id else None


async def _meta(client: httpx.AsyncClient, settings: Settings, target: Target, text: str) -> str | None:
    token = settings.meta_page_access_token or ""
    data = {"message": text, "access_token": token}
    if settings.meta_app_secret:
        data["appsecret_proof"] = hmac.new(settings.meta_app_secret.encode(), token.encode(), hashlib.sha256).hexdigest()
    r = await client.post(f"https://graph.facebook.com/{settings.meta_graph_version}/{target.ident}/comments", data=data)
    if r.status_code != 200:
        raise _refused(target.platform.title(), r)
    new_id = r.json().get("id")
    return f"https://www.facebook.com/{new_id}" if target.platform == "facebook" and new_id else None


_POSTERS = {"reddit": _reddit, "youtube": _youtube, "facebook": _meta, "instagram": _meta}


async def publish(settings: Settings, target: Target, text: str, *,
                  transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """Post once. Returns the new comment's link when the platform gives one."""
    if not capabilities(settings)[target.platform]["auto"]:
        raise PublishError(capabilities(settings)[target.platform]["reason"])
    if len(text) > MAX_LENGTH[target.platform]:
        raise PublishError(f"Reply is longer than {target.platform.title()} allows ({MAX_LENGTH[target.platform]} characters).")
    try:
        async with httpx.AsyncClient(transport=transport, timeout=30.0) as client:
            return await _POSTERS[target.platform](client, settings, target, text)
    except httpx.TimeoutException as exc:
        raise PublishError("The platform didn't answer in time. Check the thread before retrying: "
                           "the reply may already be live.") from exc
    except httpx.TransportError as exc:
        raise PublishError(f"Couldn't reach {target.platform.title()} ({type(exc).__name__}). "
                           "Your network may be blocking it.") from exc
