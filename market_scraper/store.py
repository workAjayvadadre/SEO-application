"""Saved keyword searches (the "Searches" history page).

Each search is one JSON file named by a random hex id, plus a small index of summaries so the
history list never has to open every file. With no directory, searches live in memory only.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .utils import utc_now_iso

_ID = re.compile(r"^[0-9a-f]{32}$")
SUMMARY_FIELDS = ("keyword", "source", "country", "language", "region", "total_keywords", "counts")


def valid_id(search_id: str) -> bool:
    return bool(_ID.match(search_id or ""))


class SearchStore:
    def __init__(self, root: Path | None = None, *, keep: int = 300) -> None:
        self.root = root
        self.keep = keep
        self._memory: dict[str, dict[str, Any]] = {}
        self._index: list[dict[str, Any]] = []
        if root:
            root.mkdir(parents=True, exist_ok=True)
            try:
                self._index = json.loads((root / "index.json").read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                self._index = []

    def save(self, result: dict[str, Any]) -> str:
        search_id = uuid.uuid4().hex
        created = utc_now_iso()
        result = {**result, "id": search_id, "created_at": created}
        summary = {"id": search_id, "created_at": created, **{k: result.get(k) for k in SUMMARY_FIELDS}}
        self._index.insert(0, summary)
        for old in self._index[self.keep:]:
            self._remove(old["id"])
        self._index = self._index[: self.keep]
        if self.root:
            self._write(self.root / f"{search_id}.json", result)
            self._write_index()
        else:
            self._memory[search_id] = result
        return search_id

    def list(self) -> list[dict[str, Any]]:
        return list(self._index)

    def get(self, search_id: str) -> dict[str, Any] | None:
        if not valid_id(search_id):
            return None
        if not self.root:
            return self._memory.get(search_id)
        try:
            return json.loads((self.root / f"{search_id}.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def delete(self, search_id: str) -> bool:
        if not valid_id(search_id) or not any(s["id"] == search_id for s in self._index):
            return False
        self._index = [s for s in self._index if s["id"] != search_id]
        self._remove(search_id)
        if self.root:
            self._write_index()
        return True

    def _remove(self, search_id: str) -> None:
        self._memory.pop(search_id, None)
        if self.root and valid_id(search_id):
            (self.root / f"{search_id}.json").unlink(missing_ok=True)

    def _write_index(self) -> None:
        self._write(self.root / "index.json", self._index)

    @staticmethod
    def _write(path: Path, data: Any) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)  # atomic: a crash never leaves half a file
