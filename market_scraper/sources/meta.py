"""Instagram + Facebook through the official Meta Graph API, with optional SERP discovery.

Instagram (needs an IG Business/Creator account linked to a Facebook Page):
  * Hashtag Search   -> top/recent public media for a hashtag (caption, likes, comments).
                        Meta limits each account to 30 unique hashtags per rolling 7 days.
  * Business Discovery -> public posts + follower counts of other Business/Creator profiles.
Facebook:
  * Page posts for FB_PAGE_IDS (requires the "Page Public Content Access" feature for pages you
    don't manage), filtered to posts that mention the query.
SERP discovery (`site:instagram.com <query>`) covers public posts when no Graph token is set.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

from ..models import Platform, Record, SearchRequest
from ..utils import clean_text, engagement_score, mentions_query
from .base import Source

IG_MEDIA_FIELDS = "id,caption,media_type,permalink,like_count,comments_count,timestamp"
IG_WEIGHTS = {"likes": 1.0, "comments": 1.5}
FB_WEIGHTS = {"reactions": 1.0, "comments": 1.5, "shares": 2.0}


def hashtag_for(query: str) -> str:
    return re.sub(r"[^0-9a-z_]", "", query.lower())


class _MetaSource(Source):
    site: str  # domain used for SERP discovery

    @property
    def _graph_url(self) -> str:
        return f"https://graph.facebook.com/{self.settings.meta_graph_version}"

    async def graph(self, path: str, **params: Any) -> dict[str, Any]:
        token = self.settings.meta_access_token or ""
        params["access_token"] = token
        if self.settings.meta_app_secret:  # recommended by Meta: binds the token to your app
            params["appsecret_proof"] = hmac.new(self.settings.meta_app_secret.encode(), token.encode(),
                                                 hashlib.sha256).hexdigest()
        return await self.http.get_json(f"{self._graph_url}/{path.lstrip('/')}", params=params, use_proxy=False)

    async def serp_discovery(self, request: SearchRequest) -> list[Record]:
        return await self.site_discovery(self.site, request)

    def _use_serp(self) -> bool:
        return self.settings.meta_serp_discovery and self.serp.configured


class InstagramSource(_MetaSource):
    name = Platform.INSTAGRAM
    site = "instagram.com"

    def _graph_ready(self) -> bool:
        return bool(self.settings.meta_access_token and self.settings.ig_business_account_id)

    def availability(self) -> tuple[bool, str]:
        if self._graph_ready() or self._use_serp():
            return True, ""
        return False, "Set META_ACCESS_TOKEN + IG_BUSINESS_ACCOUNT_ID, or a SERP key for discovery"

    async def collect(self, request: SearchRequest) -> list[Record]:
        tasks = []
        if self._graph_ready():
            hashtags = request.hashtags or (hashtag_for(request.query),)
            tasks += [self._hashtag_media(tag, request.limit) for tag in hashtags if tag]
            tasks += [self._business_discovery(u, request) for u in self.settings.ig_discovery_usernames]
        if self._use_serp():
            tasks.append(self.serp_discovery(request))
        return await self.settle(tasks, "instagram")

    async def _hashtag_media(self, hashtag: str, limit: int) -> list[Record]:
        user_id = self.settings.ig_business_account_id
        found = await self.graph("ig_hashtag_search", user_id=user_id, q=hashtag)
        if not found.get("data"):
            return []
        hashtag_id = found["data"][0]["id"]
        media = await self.graph(f"{hashtag_id}/{self.settings.ig_hashtag_edge}", user_id=user_id,
                                 fields=IG_MEDIA_FIELDS, limit=min(limit, 50))
        return [self._media_record(m, "hashtag_post", f"#{hashtag}") for m in media.get("data", [])]

    async def _business_discovery(self, username: str, request: SearchRequest) -> list[Record]:
        fields = ("business_discovery.username(" + username + "){username,followers_count,media_count,"
                  "media.limit(50){" + IG_MEDIA_FIELDS + "}}")
        data = await self.graph(self.settings.ig_business_account_id or "", fields=fields)
        profile = data.get("business_discovery", {})
        followers = profile.get("followers_count") or 0
        media = [m for m in profile.get("media", {}).get("data", []) if mentions_query(m.get("caption"), request.query)]
        records = []
        for m in media[: request.limit]:
            record = self._media_record(m, "profile_post", f"@{username}", author=username)
            record.metrics["followers"] = followers
            if followers:
                record.metrics["engagement_rate"] = round(
                    (record.metrics["likes"] + record.metrics["comments"]) / followers, 5)
            records.append(record)
        return records

    def _media_record(self, m: dict[str, Any], record_type: str, label: str, author: str | None = None) -> Record:
        caption = m.get("caption") or ""
        metrics = {"likes": m.get("like_count") or 0, "comments": m.get("comments_count") or 0,
                   "media_type": m.get("media_type"), "graph_id": m.get("id")}  # id: target for replies
        return Record(
            platform=self.name, record_type=record_type,
            source_url=m.get("permalink") or f"https://www.instagram.com/explore/tags/{label.lstrip('#@')}/",
            title=clean_text(f"{label}: {caption.splitlines()[0] if caption else '(no caption)'}", 200),
            content_snippet=clean_text(caption), engagement_score=engagement_score(metrics, IG_WEIGHTS),
            published_at=m.get("timestamp"), author=author, metrics=metrics,
        )


class FacebookSource(_MetaSource):
    name = Platform.FACEBOOK
    site = "facebook.com"

    def _graph_ready(self) -> bool:
        return bool(self.settings.meta_access_token and self.settings.fb_page_ids)

    def availability(self) -> tuple[bool, str]:
        if self._graph_ready() or self._use_serp():
            return True, ""
        return False, "Set META_ACCESS_TOKEN + FB_PAGE_IDS, or a SERP key for discovery"

    async def collect(self, request: SearchRequest) -> list[Record]:
        tasks = []
        if self._graph_ready():
            tasks += [self._page_posts(page, request) for page in self.settings.fb_page_ids]
        if self._use_serp():
            tasks.append(self.serp_discovery(request))
        return await self.settle(tasks, "facebook")

    async def _page_posts(self, page_id: str, request: SearchRequest) -> list[Record]:
        data = await self.graph(
            f"{page_id}/posts", limit=100,
            fields="message,permalink_url,created_time,from,shares,"
                   "reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)",
        )
        records = []
        for post in data.get("data", []):
            message = post.get("message") or ""
            if not mentions_query(message, request.query):
                continue
            metrics = {
                "reactions": post.get("reactions", {}).get("summary", {}).get("total_count", 0),
                "comments": post.get("comments", {}).get("summary", {}).get("total_count", 0),
                "shares": post.get("shares", {}).get("count", 0),
                "graph_id": post.get("id"),  # target for replies posted from the reply queue
            }
            page_name = (post.get("from") or {}).get("name") or page_id
            records.append(Record(
                platform=self.name, record_type="page_post",
                source_url=post.get("permalink_url") or f"https://www.facebook.com/{post.get('id', page_id)}",
                title=clean_text(f"{page_name}: {message.splitlines()[0] if message else ''}", 200),
                content_snippet=clean_text(message), engagement_score=engagement_score(metrics, FB_WEIGHTS),
                published_at=post.get("created_time"), author=page_name, metrics=metrics,
            ))
            if len(records) >= request.limit:
                break
        return records
