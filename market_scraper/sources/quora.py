"""Quora has no public API.

1. Discovery: `site:quora.com <query>` through the SERP API (reliable, no blocking).
2. Enrichment (optional): open each question page and read the schema.org `QAPage` JSON-LD that
   Quora embeds for search engines. It carries answers + upvote counts and is far more stable
   than Quora's obfuscated CSS classes. Use PLAYWRIGHT_ENABLED=true for best success rates.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..models import Platform, Record, SearchRequest
from ..utils import clean_text, engagement_score, rank_score
from .base import Source

ANSWER_WEIGHTS = {"upvotes": 1.0}
_NON_QUESTION_PATHS = ("/profile/", "/topic/", "/q/", "/search", "/spaces")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _walk_jsonld(node: Any):
    """Yield every dict inside a JSON-LD document, including `@graph` members."""
    for item in _as_list(node):
        if isinstance(item, dict):
            yield item
            yield from _walk_jsonld(item.get("@graph"))


def parse_qapage(html: str) -> dict[str, Any] | None:
    """Extract {question, text, answer_count, answers:[{text, upvotes, url, author, date}]}."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            doc = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk_jsonld(doc):
            types = _as_list(node.get("@type"))
            question = node.get("mainEntity") if "QAPage" in types else node if "Question" in types else None
            if not isinstance(question, dict):
                continue
            answers = []
            for ans in _as_list(question.get("acceptedAnswer")) + _as_list(question.get("suggestedAnswer")):
                if not isinstance(ans, dict) or not ans.get("text"):
                    continue
                author = ans.get("author")
                if isinstance(author, list):
                    author = author[0] if author else None
                answers.append({
                    "text": ans["text"], "upvotes": ans.get("upvoteCount") or 0, "url": ans.get("url"),
                    "date": ans.get("dateCreated"),
                    "author": author.get("name") if isinstance(author, dict) else author,
                })
            return {"question": question.get("name"), "text": question.get("text"),
                    "answer_count": question.get("answerCount"), "answers": answers}
    return None


class QuoraSource(Source):
    name = Platform.QUORA

    def availability(self) -> tuple[bool, str]:
        return (True, "") if self.serp.configured else (False, "Quora discovery needs SERPER_API_KEY or SERPAPI_API_KEY")

    async def collect(self, request: SearchRequest) -> list[Record]:
        result = await self.serp.search(f"site:quora.com {request.query}", num=request.limit,
                                        time_range=request.time_range)
        questions = [
            o for o in result.organic
            if (urlsplit(o["link"]).hostname or "").endswith("quora.com")
            and not any(p in urlsplit(o["link"]).path for p in _NON_QUESTION_PATHS)
        ][: request.limit]

        records = [
            Record(platform=self.name, record_type="question", source_url=o["link"],
                   title=clean_text(o["title"].removesuffix(" - Quora"), 300),
                   content_snippet=clean_text(o["snippet"]), engagement_score=rank_score(o["position"]),
                   published_at=o.get("date"), metrics={"rank": o["position"]})
            for o in questions
        ]

        if self.settings.quora_fetch_answers and request.comments_per_item > 0:
            pages = questions[: self.settings.quora_max_pages]
            answers = await self.settle((self._answers(o["link"], request.comments_per_item) for o in pages),
                                        "quora answers", enrichment=True)
            records += answers
        return records

    async def _answers(self, url: str, limit: int) -> list[Record]:
        qa = parse_qapage(await self.ctx.pages.fetch_html(url))
        if not qa:
            self.warnings.append(f"quora: no QAPage data on {url} (page layout changed or blocked)")
            return []
        answers = sorted(qa["answers"], key=lambda a: a["upvotes"] or 0, reverse=True)[:limit]
        title = clean_text(qa.get("question"), 300)
        return [
            Record(platform=self.name, record_type="answer", source_url=a.get("url") or url, title=title,
                   content_snippet=clean_text(a["text"]),
                   engagement_score=engagement_score({"upvotes": a["upvotes"]}, ANSWER_WEIGHTS),
                   published_at=a.get("date"), author=a.get("author"),
                   metrics={"upvotes": a["upvotes"], "question_answer_count": qa.get("answer_count")})
            for a in answers
        ]
