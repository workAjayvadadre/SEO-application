"""YouTube Data API v3: top videos (with full descriptions + stats) and recent comments.

Quota cost per run ≈ 100 (search) + 1 (videos) + 1 per comment thread page. Default quota is 10,000/day.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

from ..http import FetchError
from ..models import Platform, Record, SearchRequest
from ..utils import clean_text, engagement_score
from .base import Source

API = "https://www.googleapis.com/youtube/v3"
VIDEO_WEIGHTS = {"views": 0.5, "likes": 1.0, "comments": 1.5}
COMMENT_WEIGHTS = {"likes": 1.0, "replies": 1.0}


_WINDOWS = {"day": timedelta(days=1), "week": timedelta(weeks=1), "month": timedelta(days=30),
            "year": timedelta(days=365)}


API_DOWN_SECONDS = 15 * 60


class YouTubeSource(Source):
    name = Platform.YOUTUBE
    _api_down_until: ClassVar[float] = 0.0  # shared across runs: skip the probe while the API is known blocked

    def availability(self) -> tuple[bool, str]:
        if self.settings.youtube_api_key or self.serp.configured:
            return True, ""
        return False, "Set YOUTUBE_API_KEY (or a SERP key to find videos through Google)"

    async def collect(self, request: SearchRequest) -> list[Record]:
        """Official API when reachable; otherwise videos found through Google (no comments on that path).
        Corporate networks often block www.googleapis.com, which would otherwise cost ~2 minutes per search."""
        if self.settings.youtube_api_key and await self._api_up():
            try:
                return await self._collect_api(request)
            except FetchError as exc:
                if not self.serp.configured:
                    raise
                if exc.status is None:
                    type(self)._api_down_until = time.monotonic() + API_DOWN_SECONDS
                self.warnings.append(f"YouTube API failed ({exc}); showing videos found through Google instead.")
        elif self.settings.youtube_api_key:
            self.warnings.append("YouTube API is unreachable from this network (www.googleapis.com blocked); "
                                 "showing videos found through Google instead, without comments.")
        if not self.serp.configured:
            raise FetchError("YouTube API unreachable and no SERP key for the Google fallback")
        return await self.site_discovery("youtube.com", request, link_filter="youtube.com/")

    async def _api_up(self) -> bool:
        if time.monotonic() < type(self)._api_down_until:
            return False
        if await self.http.reachable(f"{API}/videos"):
            return True
        type(self)._api_down_until = time.monotonic() + API_DOWN_SECONDS
        return False

    async def _get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        return await self.http.get_json(f"{API}/{endpoint}", use_proxy=False,
                                        params={**params, "key": self.settings.youtube_api_key})

    async def _collect_api(self, request: SearchRequest) -> list[Record]:
        extra: dict[str, Any] = {}
        if request.time_range in _WINDOWS:
            since = datetime.now(timezone.utc) - _WINDOWS[request.time_range]
            extra["publishedAfter"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        search = await self._get(
            "search", part="snippet", q=request.query, type="video", maxResults=min(request.limit, 50),
            order=self.settings.youtube_order, regionCode=self.settings.search_country.upper(),
            relevanceLanguage=self.settings.search_language, **extra,
        )
        ids = [i["id"]["videoId"] for i in search.get("items", []) if i.get("id", {}).get("videoId")]
        if not ids:
            return []

        # search.list truncates descriptions and has no stats; videos.list fills both in one call.
        details = await self._get("videos", part="snippet,statistics", id=",".join(ids), maxResults=len(ids))
        by_id = {v["id"]: v for v in details.get("items", [])}
        videos = [by_id[i] for i in ids if i in by_id]  # keep search ranking order

        records = [self._video_record(v) for v in videos]
        if request.comments_per_item > 0:
            targets = videos[: self.settings.youtube_comment_videos]
            records += await self.settle((self._comments(v, request.comments_per_item) for v in targets),
                                         "youtube comments", enrichment=True)
        return records

    def _video_record(self, video: dict[str, Any]) -> Record:
        snippet, stats = video.get("snippet", {}), video.get("statistics", {})
        metrics = {"views": int(stats.get("viewCount", 0)), "likes": int(stats.get("likeCount", 0)),
                   "comments": int(stats.get("commentCount", 0)), "channel": snippet.get("channelTitle")}
        return Record(
            platform=self.name, record_type="video",
            source_url=f"https://www.youtube.com/watch?v={video['id']}",
            title=clean_text(snippet.get("title"), 300), content_snippet=clean_text(snippet.get("description")),
            engagement_score=engagement_score(metrics, VIDEO_WEIGHTS),
            published_at=snippet.get("publishedAt"), author=snippet.get("channelTitle"), metrics=metrics,
        )

    async def _comments(self, video: dict[str, Any], limit: int) -> list[Record]:
        try:
            data = await self._get("commentThreads", part="snippet", videoId=video["id"],
                                   maxResults=min(limit, 100), order="time", textFormat="plainText")
        except FetchError as exc:
            if exc.status in (403, 404) and exc.body and "commentsDisabled" in exc.body:
                return []  # creator turned comments off: not an error
            raise

        title = clean_text(video.get("snippet", {}).get("title"), 250)
        records = []
        for thread in data.get("items", []):
            top = thread["snippet"]["topLevelComment"]
            c = top["snippet"]
            metrics = {"likes": c.get("likeCount", 0), "replies": thread["snippet"].get("totalReplyCount", 0)}
            records.append(Record(
                platform=self.name, record_type="comment",
                source_url=f"https://www.youtube.com/watch?v={video['id']}&lc={top['id']}",
                title=f"Comment on: {title}", content_snippet=clean_text(c.get("textDisplay")),
                engagement_score=engagement_score(metrics, COMMENT_WEIGHTS),
                published_at=c.get("publishedAt"), author=c.get("authorDisplayName"), metrics=metrics,
            ))
        return records
