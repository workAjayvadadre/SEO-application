"""Reddit, in one of two modes.

1. Official OAuth API (application-only client-credentials grant), when REDDIT_CLIENT_ID/SECRET are
   set. Create a "script" app at https://www.reddit.com/prefs/apps. Free tier allows ~100 requests/minute.
2. SERP fallback, when only a SERP key is set: `site:reddit.com <query>` discovers threads, then each
   top thread's public `.json` is read (best effort) for real scores and top comments. If Reddit
   blocks that, the SERP results are still returned and the failure becomes a warning.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import urlsplit

from ..models import Platform, Record, SearchRequest
from ..utils import clean_text, engagement_score, epoch_to_iso, rank_score
from .base import Source

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"
POST_WEIGHTS = {"score": 1.0, "num_comments": 1.5}
COMMENT_WEIGHTS = {"score": 1.0, "replies": 0.5}
PUBLIC = "https://www.reddit.com"
_DEAD_BODIES = {"[deleted]", "[removed]", ""}
_THREAD_PATH = re.compile(r"^/r/([^/]+)/comments/([a-z0-9]+)", re.I)


def thread_from_url(url: str) -> tuple[str, str] | None:
    """(subreddit, post id) for a Reddit thread URL, else None (subreddit pages, user pages, ...)."""
    parts = urlsplit(url)
    if not (parts.hostname or "").endswith("reddit.com"):
        return None
    match = _THREAD_PATH.match(parts.path)
    return (match.group(1), match.group(2).lower()) if match else None


class RedditSource(Source):
    name = Platform.REDDIT

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def _use_api(self) -> bool:
        return bool(self.settings.reddit_client_id and self.settings.reddit_client_secret)

    def availability(self) -> tuple[bool, str]:
        if self._use_api or self.serp.configured:
            return True, ""
        return False, "Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET, or a SERP key (SERPER_API_KEY)"

    async def _headers(self) -> dict[str, str]:
        async with self._token_lock:
            if not self._token or time.monotonic() >= self._token_expires:
                data = await self.http.json(
                    "POST", TOKEN_URL, use_proxy=False,
                    data={"grant_type": "client_credentials"},
                    auth=(self.settings.reddit_client_id or "", self.settings.reddit_client_secret or ""),
                    headers={"User-Agent": self.settings.reddit_user_agent},
                )
                if "access_token" not in data:
                    raise RuntimeError(f"Reddit auth failed: {data.get('error', data)}")
                self._token = data["access_token"]
                self._token_expires = time.monotonic() + float(data.get("expires_in", 3600)) - 60
        return {"Authorization": f"bearer {self._token}", "User-Agent": self.settings.reddit_user_agent}

    async def collect(self, request: SearchRequest) -> list[Record]:
        return await (self._collect_api(request) if self._use_api else self._collect_serp(request))

    # ------------------------------------------------------------ SERP fallback

    async def _collect_serp(self, request: SearchRequest) -> list[Record]:
        result = await self.serp.search(f"site:reddit.com {request.query}", num=request.limit,
                                        time_range=request.time_range)
        threads: dict[str, dict[str, Any]] = {}
        for o in result.organic:
            parsed = thread_from_url(o["link"])
            if parsed and parsed[1] not in threads:
                threads[parsed[1]] = {**o, "subreddit": parsed[0], "id": parsed[1]}
        hits = list(threads.values())[: request.limit]

        records = {h["id"]: self._serp_record(h) for h in hits}
        comments: list[Record] = []
        top = hits[: self.settings.reddit_comment_posts]
        if top and request.comments_per_item > 0:
            enriched = await asyncio.gather(
                *(self._public_thread(h["id"], request.comments_per_item) for h in top), return_exceptions=True)
            for h, res in zip(top, enriched):
                if isinstance(res, asyncio.CancelledError):
                    raise res
                if isinstance(res, BaseException):
                    self.warnings.append(f"reddit thread {h['id']}: {res}")
                    self.log.warning("reddit public thread %s failed: %s", h["id"], res)
                    continue
                post, thread_comments = res
                if post is not None:
                    records[h["id"]] = post  # real score/comment counts beat the rank proxy
                comments += thread_comments
        return list(records.values()) + comments

    def _serp_record(self, hit: dict[str, Any]) -> Record:
        return Record(
            platform=self.name, record_type="post", source_url=hit["link"],
            title=clean_text(re.sub(r"\s*:\s*r/\w+\s*$", "", hit["title"]).removesuffix(" - Reddit"), 300),
            content_snippet=clean_text(hit.get("snippet")), engagement_score=rank_score(hit.get("position")),
            published_at=hit.get("date"), metrics={"rank": hit.get("position"), "subreddit": hit["subreddit"]},
        )

    async def _public_thread(self, post_id: str, limit: int) -> tuple[Record | None, list[Record]]:
        data = await self.http.get_json(
            f"{PUBLIC}/comments/{post_id}.json", headers={"User-Agent": self.settings.reddit_user_agent},
            params={"sort": "top", "limit": limit, "depth": 1, "raw_json": 1},
        )
        if not isinstance(data, list) or not data:
            raise RuntimeError("unexpected response shape")
        children = data[0].get("data", {}).get("children", [])
        post = children[0]["data"] if children and children[0].get("kind") == "t3" else None
        post_record = self._post_record(post) if post else None
        return post_record, self._comment_records(data, post or {}, limit)

    # ------------------------------------------------------------ official API

    async def _collect_api(self, request: SearchRequest) -> list[Record]:
        listing = await self.http.get_json(
            f"{API}/search", use_proxy=False, headers=await self._headers(),
            params={"q": request.query, "sort": self.settings.reddit_sort, "t": request.time_range or self.settings.reddit_time_filter,
                    "limit": min(request.limit, 100), "type": "link", "raw_json": 1},
        )
        posts = [c["data"] for c in listing.get("data", {}).get("children", []) if c.get("kind") == "t3"]
        records = [self._post_record(p) for p in posts]

        # Comments are where the pain points live: pull them for the most-discussed threads.
        threads = sorted((p for p in posts if p.get("num_comments")), key=lambda p: p.get("score", 0), reverse=True)
        threads = threads[: self.settings.reddit_comment_posts]
        if threads and request.comments_per_item > 0:
            records += await self.settle(
                (self._comments(p, request.comments_per_item) for p in threads), "reddit comments",
                enrichment=True)
        return records

    def _post_record(self, post: dict[str, Any]) -> Record:
        body = post.get("selftext") or (f"[link] {post['url']}" if post.get("url") else "")
        metrics = {
            "score": post.get("score", 0), "num_comments": post.get("num_comments", 0),
            "upvote_ratio": post.get("upvote_ratio"), "subreddit": post.get("subreddit"),
        }
        return Record(
            platform=self.name, record_type="post",
            source_url=f"https://www.reddit.com{post.get('permalink', '')}",
            title=clean_text(post.get("title"), 300), content_snippet=clean_text(body),
            engagement_score=engagement_score(metrics, POST_WEIGHTS),
            published_at=epoch_to_iso(post.get("created_utc")), author=post.get("author"), metrics=metrics,
        )

    async def _comments(self, post: dict[str, Any], limit: int) -> list[Record]:
        data = await self.http.get_json(
            f"{API}/comments/{post['id']}", use_proxy=False, headers=await self._headers(),
            params={"sort": "top", "limit": limit, "depth": 1, "raw_json": 1},
        )
        return self._comment_records(data, post, limit)

    def _comment_records(self, data: Any, post: dict[str, Any], limit: int) -> list[Record]:
        children = data[1]["data"]["children"] if isinstance(data, list) and len(data) > 1 else []
        comments = [
            c["data"] for c in children
            if c.get("kind") == "t1" and not c["data"].get("stickied")
            and (c["data"].get("body") or "").strip() not in _DEAD_BODIES
        ]
        comments.sort(key=lambda c: c.get("score", 0), reverse=True)

        records = []
        for c in comments[:limit]:
            replies = c.get("replies")
            reply_count = len(replies["data"]["children"]) if isinstance(replies, dict) else 0
            metrics = {"score": c.get("score", 0), "replies": reply_count, "subreddit": c.get("subreddit")}
            if post.get("permalink"):  # links the comment back to its thread in open queries
                metrics["question_url"] = f"https://www.reddit.com{post['permalink']}"
            records.append(Record(
                platform=self.name, record_type="comment",
                source_url=f"https://www.reddit.com{c.get('permalink', '')}",
                title=clean_text(f"r/{post.get('subreddit')}: {post.get('title')}", 300),
                content_snippet=clean_text(c.get("body")),
                engagement_score=engagement_score(metrics, COMMENT_WEIGHTS),
                published_at=epoch_to_iso(c.get("created_utc")), author=c.get("author"), metrics=metrics,
            ))
        return records
