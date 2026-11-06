"""Answers pasted by hand for open queries whose pages the app could not read (Quora/Reddit blocks).

Keyed by the question's link plus its text, so a pasted answer reappears whenever the same question
shows up in a later search. Stored as one small JSON file; with no path, it lives in memory (tests).
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .utils import utc_now_iso

MAX_ANSWERS_PER_QUESTION = 20
MAX_ANSWER_CHARS = 5000
MAX_QUESTIONS = 5000

_REDDIT_ID = re.compile(r"/comments/([a-z0-9]+)", re.I)


def url_key(url: str | None) -> str:
    """Normalise a link so the same page matches across sources (www./old./m., trailing slash, query)."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "old.", "m."):
        host = host.removeprefix(prefix)
    if host.endswith("reddit.com") and (m := _REDDIT_ID.search(parts.path)):
        return f"reddit.com/comments/{m.group(1).lower()}"  # SERP links and API permalinks differ in the slug
    return f"{host}{parts.path.rstrip('/')}".lower()


def answer_key(source_url: str | None, question: str) -> str:
    # Google "People also ask" items can share one search URL, so the question text is part of the key.
    text = re.sub(r"\W+", " ", (question or "").casefold()).strip()[:160]
    return f"{url_key(source_url)}|{text}"


class ManualAnswers:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        if path:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self._data = loaded if isinstance(loaded, dict) else {}
            except (FileNotFoundError, ValueError):
                self._data = {}

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._data)

    def get(self, key: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._data.get(key, {}).get("answers", []))

    def set(self, source_url: str, question: str, answers: list[str]) -> dict[str, Any]:
        """Replace the pasted answers for one question. An empty list removes the question."""
        key = answer_key(source_url, question)
        cleaned = [a.strip()[:MAX_ANSWER_CHARS] for a in answers if a and a.strip()][:MAX_ANSWERS_PER_QUESTION]
        with self._lock:
            previous = {a["text"]: a for a in self._data.get(key, {}).get("answers", [])}
            if cleaned:
                now = utc_now_iso()
                self._data[key] = {
                    "question": question[:300], "source_url": source_url, "updated_at": now,
                    "answers": [previous.get(t) or {"text": t, "added_at": now} for t in cleaned],
                }
                if len(self._data) > MAX_QUESTIONS:  # drop the oldest
                    oldest = sorted(self._data, key=lambda k: self._data[k].get("updated_at", ""))
                    for k in oldest[: len(self._data) - MAX_QUESTIONS]:
                        self._data.pop(k, None)
            else:
                self._data.pop(key, None)
            self._write()
            return {"key": key, "answers": list(self._data.get(key, {}).get("answers", []))}

    def _write(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: a crash never leaves half a file
